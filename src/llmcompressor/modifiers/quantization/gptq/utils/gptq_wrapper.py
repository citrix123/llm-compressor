import time
from typing import Tuple

from compressed_tensors.quantization import ActivationOrdering, QuantizationStrategy
from compressed_tensors.quantization.lifecycle.forward import fake_quantize

from llmcompressor.modifiers.utils import SPARSITY_THRESHOLD
from llmcompressor.modifiers.utils.compression_wrapper import ModuleCompressionWrapper
from llmcompressor.observers import Observer
from llmcompressor.pytorch.utils.helpers import tensor_sparsity
from llmcompressor.utils import getattr_chain
from llmcompressor.utils.metric_logging import get_GPU_memory_usage, get_layer_size_mb

try:
    import transformers
except ImportError as err:
    transformers = None
    transformers_err = err

import math
from copy import copy

import torch
import torch.nn as nn
from compressed_tensors.utils import (
    get_offloaded_device,
    is_module_offloaded,
    update_parameter_data,
    update_prefix_dict,
)
from loguru import logger

__all__ = ["GPTQWrapper"]


class GPTQWrapper(ModuleCompressionWrapper):
    """
    Runs GPTQ on a single module that contains no sub-modules

    Lifecycle:
        - add_batch
        - compress
        - free

    :param name: name of module to run compression on
    :param layer: module to run compression on
    """

    def __init__(self, name, layer):
        super().__init__(name=name, layer=layer)

        # for Hessian calculation
        self.register_buffer(
            "H",
            torch.zeros(
                (self.columns, self.columns), device=self.dev, dtype=torch.float32
            ),
        )

    def add_batch(self, inp: torch.Tensor, out: torch.Tensor):
        """
        Add a batch of layer input and output data to the Hessian calculation

        :param inp: tensor containing layer input
        :param out: tensor containing layer output
        """
        if len(inp.shape) == 2:
            inp = inp.unsqueeze(0)
        tmp = inp.shape[0]
        if isinstance(self.layer, nn.Linear) or isinstance(
            self.layer, transformers.Conv1D
        ):
            if len(inp.shape) == 3:
                inp = inp.reshape((-1, inp.shape[-1]))
            inp = inp.t()
        self.H *= self.nsamples / (self.nsamples + tmp)
        self.nsamples += tmp
        inp = inp.to(dtype=self.H.dtype)
        inp = math.sqrt(2 / self.nsamples) * inp
        self.H += inp.matmul(inp.t())

    def compress(
        self,
        blocksize: int = 128,
        percdamp: float = 0.01,
    ):
        """
        Run pruning and quantization(if applicable) on the layer up to the target
        sparsity value.

        :param blocksize: Number of columns to compress in one pass
        :param percdamp: Amount of dampening to apply to H, as a fraction of the
            diagonal norm
        """
        args_loc = "quantization_scheme.weights"
        quant_args = getattr_chain(self.layer, args_loc, None)
        if quant_args is None:
            logger.debug(f"Skipping unquantized layer {self.name}...")
            return

        if is_module_offloaded(self.layer):
            self.layer._hf_hook.pre_forward(self.layer)

        strategy = quant_args.strategy
        actorder = quant_args.actorder
        final_shape = self.layer.weight.shape
        final_dtype = self.layer.weight.dtype
        W = self.layer.weight.data.clone()

        # create observer for calculating quantization parameters
        observer = Observer.load_from_registry(
            quant_args.observer,
            quantization_args=quant_args,
            averaging_constant=1.0,  # ignore moving average
        )

        # standardize shape and dtype
        if isinstance(self.layer, nn.Conv2d):
            W = W.flatten(1)
        elif isinstance(self.layer, transformers.Conv1D):
            W.transpose_(0, 1)
        W = W.float()

        tick = time.time()

        if strategy == QuantizationStrategy.GROUP:
            # mapping from column index to group index
            g_idx = (
                torch.arange(self.columns, device=W.device, dtype=torch.int)
                // quant_args.group_size
            )

            if actorder == ActivationOrdering.GROUP:
                # permute by activation order first, then update groups
                W, self.H, perm = self._apply_activation_ordering(W, self.H)
                scale, zero_point = observer(W, g_idx=None)

                # use identity g_idx (invert permutation later)

            elif actorder == ActivationOrdering.WEIGHT:
                # update groups first, then permute by activation order
                scale, zero_point = observer(W, g_idx=None)
                W, self.H, perm = self._apply_activation_ordering(W, self.H)

                # permute g_idx to maintain identity mapping after unpermutation
                g_idx = g_idx[perm]

            else:
                scale, zero_point = observer(W, g_idx=None)
        else:
            scale, zero_point = observer(W, g_idx=None)

        # sparsity mask
        sparsity = tensor_sparsity(W)
        preserve_zeros = sparsity >= SPARSITY_THRESHOLD
        W_nz_mask = (
            (~torch.isclose(W, torch.zeros(1, device=W.device).float())).float()
            if preserve_zeros
            else None
        )

        # mask dead hessian values
        dead = torch.diag(self.H) == 0
        self.H[dead, dead] = 1
        W[:, dead] = 0

        Losses = torch.zeros(self.rows, device=self.dev)

        # compute inverse hessian in place to save memory
        try:
            damp = percdamp * torch.mean(torch.diag(self.H))
            diag = torch.arange(self.columns, device=self.dev)
            self.H[diag, diag] += damp
            self.H = torch.linalg.cholesky(self.H)
            self.H = torch.cholesky_inverse(self.H)
            self.H = torch.linalg.cholesky(self.H, upper=True)
            Hinv = self.H
        except torch._C._LinAlgError:
            raise ValueError(
                "Failed to invert hessian due to numerical instability. Consider "
                "increasing GPTQModifier.dampening_frac, increasing the number "
                "of calibration samples, or shuffling the calibration dataset"
            )

        # See section 3.4 of https://arxiv.org/abs/2203.07259
        for i1 in range(0, self.columns, blocksize):
            i2 = min(i1 + blocksize, self.columns)
            count = i2 - i1

            W1 = W[:, i1:i2].clone()
            Q1 = torch.zeros_like(W1)
            Err1 = torch.zeros_like(W1)
            Losses1 = torch.zeros_like(W1)
            Hinv1 = Hinv[i1:i2, i1:i2]

            if preserve_zeros:
                W1_nz_mask = W_nz_mask[:, i1:i2]

            for i in range(count):
                w = W1[:, i]
                d = Hinv1[i, i]
                q = w.clone()

                # quantize column
                if strategy == QuantizationStrategy.TENSOR:
                    q = fake_quantize(
                        q,
                        scale,
                        zero_point,
                        self.layer.quantization_scheme.weights,
                    )
                elif strategy == QuantizationStrategy.CHANNEL:
                    q = fake_quantize(
                        q,
                        scale[:, 0],
                        zero_point[:, 0],
                        quant_args,
                    )
                elif strategy == QuantizationStrategy.GROUP:
                    # get the group index for the current column
                    column_idx = i1 + i
                    group_index = g_idx[column_idx]

                    # update quantization parameters to reflect changes
                    # resulting from previous blocks
                    if (
                        actorder != ActivationOrdering.WEIGHT
                        and column_idx % quant_args.group_size == 0
                    ):
                        _scale, _zero_point = observer.get_qparams_along_dim(
                            W[:, g_idx == group_index], dim=0
                        )
                        scale[:, group_index] = _scale[:, 0]
                        zero_point[:, group_index] = _zero_point[:, 0]

                    # Since we're only applying quantization to a slice, this
                    # ends up being a channelwise application
                    altered_qargs = copy(quant_args)
                    altered_qargs.strategy = QuantizationStrategy.CHANNEL
                    q = fake_quantize(
                        q,
                        scale[:, group_index],
                        zero_point[:, group_index],
                        altered_qargs,
                    )
                else:
                    raise ValueError(
                        "Quantization strategy is not supported for GPTQ: "
                        f"{strategy}"
                    )

                # propagate column error
                Q1[:, i] = q
                Losses1[:, i] = (w - q) ** 2 / d**2

                err1 = (w - q) / d
                w1_err = err1.unsqueeze(1).matmul(Hinv1[i, i:].unsqueeze(0))
                if preserve_zeros:
                    W1[:, i:] -= w1_err * W1_nz_mask[:, i:]
                else:
                    W1[:, i:] -= w1_err
                Err1[:, i] = err1

            # propagate block error
            W[:, i1:i2] = Q1
            Losses += torch.sum(Losses1, 1) / 2

            w_err = Err1.matmul(Hinv[i1:i2, i2:])
            if preserve_zeros:
                W[:, i2:] -= w_err * W_nz_mask[:, i2:]
            else:
                W[:, i2:] -= w_err

        if "METRIC" in logger._core.levels.keys():
            self._log_metrics(tick, Losses)

        if strategy == QuantizationStrategy.GROUP:
            if actorder == ActivationOrdering.WEIGHT:
                # restore original permutation
                invperm = torch.argsort(perm)
                W = W[:, invperm]

            elif actorder == ActivationOrdering.GROUP:
                # restore original permutation
                invperm = torch.argsort(perm)
                W = W[:, invperm]
                g_idx = g_idx[invperm]

                # only save g_idx if mapping is not identity
                update_parameter_data(self.layer, g_idx, "weight_g_idx")

        if isinstance(self.layer, transformers.Conv1D):
            W.transpose_(0, 1)
        W = W.reshape(final_shape).to(final_dtype)

        update_parameter_data(self.layer, scale, "weight_scale")
        update_parameter_data(self.layer, zero_point, "weight_zero_point")

        # This is a bit hacky, but FSDP updates only work if we change
        # the weight in place, clone() or direct assignment won't work
        self.layer.weight -= self.layer.weight
        self.layer.weight += W

        if is_module_offloaded(self.layer):
            device = get_offloaded_device(self.layer)
            update_prefix_dict(self.layer, "weight", self.layer.weight.to(device))
            self.layer._hf_hook.post_forward(self.layer, None)

    def free(self):
        """
        Free the Hessian memory after the layer is complete
        """
        delattr(self, "H")
        super().free()

    def _apply_activation_ordering(
        self, W: torch.Tensor, H: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Permute weight and hessian in order of greatest outupt activations

        :param W: weight to permute
        """
        perm = torch.argsort(torch.diag(H), descending=True)
        return W[:, perm], H[perm][:, perm], perm

    def _log_metrics(self, start_tick: float, losses: torch.Tensor):
        """
        Log metrics related to compression algorithm

        :param start_tick: time when algorithm started"
        :param losses: loss as result of algorithm
        """
        patch = logger.patch(lambda r: r.update(function="compress"))
        patch.log("METRIC", "time %.2f" % (time.time() - start_tick))
        patch.log("METRIC", "error %.2f" % torch.sum(losses).item())

        gpu_usage = get_GPU_memory_usage()
        if len(gpu_usage) > 0:
            for i in range(len(gpu_usage)):
                perc = gpu_usage[i][0] * 100
                total_memory = int(gpu_usage[i][1])  # GB
                patch.log(
                    "METRIC",
                    (
                        f"GPU {i} | usage: {perc:.2f}%"
                        f" | total memory: {total_memory} GB"
                    ),
                )

        patch.log(
            "METRIC",
            f"Compressed layer size: {get_layer_size_mb(self.layer)} MB",
        )
