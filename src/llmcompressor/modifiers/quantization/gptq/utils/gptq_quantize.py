import math
from copy import copy
from typing import Tuple, Union, Optional, Type

import torch
import transformers
from compressed_tensors.quantization import (
    ActivationOrdering,
    QuantizationArgs,
    QuantizationStrategy,
    fake_quantize,
)

from llmcompressor.modifiers.utils import SPARSITY_THRESHOLD
from llmcompressor.pytorch.utils.helpers import tensor_sparsity

GPTQ_PRECISION = torch.float32


def add_batch(H: torch.Tensor, nsamples: int , module: torch.nn.Module, inp: torch.Tensor):
    """
    Add a batch of layer input and output data to the Hessian calculation
    """
    if len(inp.shape) == 2:
        inp = inp.unsqueeze(0)
    tmp = inp.shape[0]
    if isinstance(module, torch.nn.Linear) or isinstance(
        module, transformers.Conv1D
    ):
        if len(inp.shape) == 3:
            inp = inp.reshape((-1, inp.shape[-1]))
        inp = inp.t()
    H *= nsamples / (nsamples + tmp)
    nsamples += tmp
    inp = inp.to(dtype=H.dtype)
    inp = math.sqrt(2 / nsamples) * inp
    H += inp.matmul(inp.t())

    return H, nsamples


def compute_hessian(inp: torch.Tensor, module_class, device) -> torch.Tensor:
    """
    Calculate the hessian with respect to the module inputs

    :param inp: module inputs
    :param module_class: class of module, likely torch.nn.Linear
    :return: hessian w.r.t. module inputs
    """
    inp = inp.to(device=device)
    if len(inp.shape) == 2:
        inp = inp.unsqueeze(0)

    nsamples = inp.shape[0]  # note this is the number of dataset samples, not
    # multiplied by the sequence length

    if module_class in (torch.nn.Linear, transformers.Conv1D):
        if len(inp.shape) == 3:
            inp = inp.reshape((-1, inp.shape[-1]))
        inp = inp.t()

    inp = inp.to(dtype=GPTQ_PRECISION)
    inp = math.sqrt(2 / nsamples) * inp
    return inp.matmul(inp.t())


def invert_hessian(H: torch.Tensor, percdamp: float) -> torch.Tensor:
    """
    Performs in-place inversion of the hessian in order to save memory

    :param H: hessian being inverted
    :param percdamp: dampening factor on hessian diagonal
    :return: inverted hessian
    """
    damp = percdamp * torch.mean(torch.diag(H))
    diag = torch.arange(H.shape[0], device=H.device)
    H[diag, diag] += damp
    H = torch.linalg.cholesky(H)
    H = torch.cholesky_inverse(H)
    H = torch.linalg.cholesky(H, upper=True)
    return H


def compute_scale_zero_point(
    W: torch.Tensor,
    quant_args: QuantizationArgs,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Compute the scale and zero point of a module weight
    TODO: revisit after observers refactor

    :param W: module weight
    :param quant_args: quantization arguments which determine how quantization
        parameters are calculated
    :return: scale and zero_point
    """
    # TODO: revisit after observers refactor

    scale, zero_point = quant_args.get_observer()(W, g_idx=None)
    scale = scale.to(dtype=W.dtype)
    zero_point = zero_point.to(dtype=quant_args.pytorch_dtype())
    return scale, zero_point


def quantize_weight(
    weight: torch.Tensor,
    inp: torch.Tensor,
    quant_args: QuantizationArgs,
    blocksize: int = 128,
    percdamp: float = 0.01,
    module_class: Type[torch.nn.Module] = torch.nn.Linear,
    weight_original: Optional[torch.Tensor] = None,
) -> Tuple[float, torch.Tensor, torch.Tensor, Union[torch.Tensor, None], torch.Tensor]:
    """
    Quantize a module weight according to the GPTQ algorithm

    TODO
    :param weight: weight being quantized
    :param inp: module inputs used to calculate hessian
    :param quant_args: quantization arguments used to find quantization parameters
    :param blocksize: chunk size of quantization updates
    :param percdamp: dampening factor on hessian diagonal
    :param module_class: class of module, likely torch.nn.Linear
    :return: loss, quantized_weight, scale, zero_point, g_idx
    """
    strategy = quant_args.strategy
    actorder = quant_args.actorder
    final_shape = weight.shape
    final_dtype = weight.dtype
    W = weight.data.clone()

    H = compute_hessian(inp, module_class, device=weight.device)

    # standardize shape and dtype
    if module_class == torch.nn.Conv2d:
        W = W.flatten(1)
    elif module_class == transformers.Conv1D:
        W.transpose_(0, 1)
    W = W.to(dtype=GPTQ_PRECISION)
    num_rows = W.shape[0]
    num_columns = W.shape[1]

    if strategy == QuantizationStrategy.GROUP:
        # mapping from column index to group index
        g_idx = (
            torch.arange(num_columns, device=W.device, dtype=torch.int)
            // quant_args.group_size
        )

        if actorder == ActivationOrdering.GROUP:
            # permute by activation order first, then update groups
            W, H, perm = _apply_activation_ordering(W, H)
            scale, zero_point = compute_scale_zero_point(W, quant_args)

            # use identity g_idx (invert permutation later)

        elif actorder == ActivationOrdering.WEIGHT:
            # update groups first, then permute by activation order
            scale, zero_point = compute_scale_zero_point(W, quant_args)
            W, H, perm = _apply_activation_ordering(W, H)

            # permute g_idx to maintain identity mapping after unpermutation
            g_idx = g_idx[perm]

        else:
            scale, zero_point = compute_scale_zero_point(W, quant_args)
    else:
        scale, zero_point = compute_scale_zero_point(W, quant_args)

    # sparsity mask
    sparsity = tensor_sparsity(W)
    preserve_zeros = sparsity >= SPARSITY_THRESHOLD
    W_nz_mask = (
        (~torch.isclose(W, torch.zeros(1, device=W.device).float())).float()
        if preserve_zeros
        else None
    )

    losses = torch.zeros(num_rows, device=weight.device)

    # mask dead hessian values
    dead = torch.diag(H) == 0
    H[dead, dead] = 1
    W[:, dead] = 0

    # compute inverse hessian in place to save memory
    # TODO: check in place
    Hinv = invert_hessian(H, percdamp)

    # See section 3.4 of https://arxiv.org/abs/2203.07259
    for i1 in range(0, num_columns, blocksize):
        i2 = min(i1 + blocksize, num_columns)
        count = i2 - i1

        W1 = W[:, i1:i2].clone()
        Q1 = torch.zeros_like(W1)
        Err1 = torch.zeros_like(W1)
        losses1 = torch.zeros_like(W1)
        Hinv1 = Hinv[i1:i2, i1:i2]

        if preserve_zeros:
            W1_nz_mask = W_nz_mask[:, i1:i2]

        for i in range(count):
            w = weight_original[:, i]
            d = Hinv1[i, i]
            q = W1[:, i].clone()

            # quantize column
            if strategy == QuantizationStrategy.TENSOR:
                q = fake_quantize(
                    q,
                    scale,
                    zero_point,
                    quant_args,
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
                    f"Quantization strategy is not supported for GPTQ: {strategy}"
                )

            # propagate column error
            Q1[:, i] = q
            losses1[:, i] = (w - q) ** 2 / d**2

            err1 = (w - q) / d
            w1_err = err1.unsqueeze(1).matmul(Hinv1[i, i:].unsqueeze(0))
            if preserve_zeros:
                W1[:, i:] -= w1_err * W1_nz_mask[:, i:]
            else:
                W1[:, i:] -= w1_err
            Err1[:, i] = err1

        # propagate block error
        W[:, i1:i2] = Q1
        losses += torch.sum(losses1, 1) / 2

        w_err = Err1.matmul(Hinv[i1:i2, i2:])
        if preserve_zeros:
            W[:, i2:] -= w_err * W_nz_mask[:, i2:]
        else:
            W[:, i2:] -= w_err

    has_gidx = False
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
            has_gidx = True

    if not has_gidx:
        g_idx = None

    if module_class == transformers.Conv1D:
        W.transpose_(0, 1)
    W = W.reshape(final_shape).to(final_dtype)

    loss = torch.sum(losses).item()
    return loss, W, scale, zero_point, g_idx


def _apply_activation_ordering(
    W: torch.Tensor, H: torch.Tensor
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Permute weight and hessian in order of greatest outupt activations

    :param W: weight to permute
    :param H: hessian used to determine activation ordering
    :return: permuted weight, permuted hessian, permutation map
    """
    perm = torch.argsort(torch.diag(H), descending=True)
    return W[:, perm], H[perm][:, perm], perm