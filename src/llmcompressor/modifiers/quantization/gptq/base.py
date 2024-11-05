import warnings
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import math
from compressed_tensors.quantization import (
    QuantizationScheme,
)
from llmcompressor.modifiers.quantization.calibration import freeze_module_quantization
from loguru import logger
from pydantic import Field, PrivateAttr, field_validator

from llmcompressor.core import State
from llmcompressor.modifiers import Modifier, ModifierFactory
from llmcompressor.modifiers.quantization.gptq.utils.gptq_quantize import quantize_weight
from llmcompressor.modifiers.quantization.quantization.base import QuantizationModifier
from llmcompressor.modifiers.utils.hooks import LayerCompressorMixin
from llmcompressor.modifiers.utils.pytorch_helpers import run_calibration_forward
from llmcompressor.observers.base import Observer
from llmcompressor.transformers.finetune.data.data_helpers import (
    create_batch_dataloader,
)
from llmcompressor.utils.fsdp.helpers import delete_offload_parameter, register_offload_parameter, update_offload_parameter
from llmcompressor.utils.helpers import (
    align_module,
    calibration_forward_context,
    getattr_chain,
)
from compressed_tensors.quantization import (
    fake_quantize,
)

from llmcompressor.utils.pytorch.module import qat_active

__all__ = ["GPTQModifier"]


class GPTQModifier(Modifier, LayerCompressorMixin):
    """
    Modifier for applying the one-shot OBCQ algorithm to a model

    Lifecycle:
        - on_initialize
            - initialize_compression()
                - compressible_layers()
                - LayerCompressor.pre_compress()
            - apply_compression()
                - run_calibration_forward()
                - LayerCompressor.compress()
                - LayerCompressor.post_compress()
                - LayerCompressor.revert_layer_wrappers()
    | Sample yaml:
    | test_stage:
    |    obcq_modifiers:
    |      GPTQModifier:
    |          true_sequential: False
    |          dampening_frac: 0.001
    |          block_size: 128
    |          config_groups:
    |            group_0:
    |                targets:
    |                  - "Linear"
    |                input_activations: null
    |                output_activations: null
    |                weights:
    |                    num_bits: 8
    |                    type: "int"
    |                    symmetric: true
    |                    strategy: "tensor"
    |                    group_size: 128
    |                    actorder: False


    :param sequential_update: Whether or not to update weights sequentially by layer.
        This option is depreciated and setting to False is no longer supported
    :param sequential_targets: list of layer names to compress during GPTQ, or
        '__ALL__' to compress every layer in the model
    :param block_size: Used to determine number of columns to compress in one pass
    :param quantize: Set to True to quantize using an existing quantization modifier,
        or pass in the configuration for a quantization modifier if one does not
        already exist in the recipe
    :param dampening_frac: Amount of dampening to apply to H, as a fraction of the
        diagonal norm
    :param config_groups: [Used, if a quantization modifier is not specified],
        dictionary specifying quantization schemes to apply to target
        modules. Modules not matching a scheme target will NOT be quantized.
    :param ignore: [Used, if a quantization modifier is not specified]
        optional list of module class names or submodule names to not
        quantize even if they match a target in config_groups. Defaults to empty list.
    :param disable_quantization_observer_epoch: [Used, if a quantization modifier is
        not specified] Epoch to disable updates to the module
        quantization observers. At this point, quantized weights and zero points will
        not be updated. Leave None to not disable observers during QAT. Default is None
    :param num_calibration_steps: Number of steps to run post training calibration for.
        When None, the entire calibration_dataloader is used
    :param scheme: [Used, if a quantization modifier is not specified], the quantization
        scheme to apply to the model, this is a dictionary that supports all keys from
        QuantizationScheme except targets, which will be set to the targets parameter
        set at the modifier level. Can also be set to a dictionary of the format
        `preset_scheme_name: targets` for example: `W8A8: ['Linear']` for weight 8 bit
        or a string of a preset scheme if targets is provided
        and activation 8 bit quantization on the Linear layers.
    """

    sequential_update: bool = True  # DEPRECIATED
    batch_size: int = -1
    sequential_targets: Union[str, List[str], None] = None
    block_size: int = 128
    dampening_frac: Optional[float] = 0.01
    quantize: Union[bool, Dict] = True

    # arguments used for quant modifier
    config_groups: Optional[Dict[str, QuantizationScheme]] = None
    scheme: Optional[Union[str, Dict[str, Any]]] = None
    targets: Union[str, List[str], None] = None
    ignore: List[str] = Field(default_factory=list)
    num_calibration_steps: Optional[int] = None
    disable_quantization_observer_epoch: Optional[float] = None

    _quantization_modifier: Optional[QuantizationModifier] = PrivateAttr()
    _num_batches: int = PrivateAttr()

    @field_validator("sequential_update", mode="before")
    def validate_sequential_update(cls, value: bool) -> bool:
        if not value:
            warnings.warn(
                "`sequential_update=False` is no longer supported, setting "
                "sequential_update=True",
                DeprecationWarning,
            )

        return True

    def on_initialize_structure(self, state: State, **kwargs):
        """
        Check the model's quantization state matches that expected by this modifier,
        adding a default quantization scheme if needed

        TODO: Depreciate and fold into `on_initialize`

        :param state: session state storing input model and calibration data
        """
        quantization_already_active = qat_active(state.model)
        if isinstance(self.quantize, bool):
            if not self.quantize and quantization_already_active:
                logger.warning(
                    "GPTQ quantization is set to False, but a "
                    "quantization modifier is already active on the model "
                    "resetting quantize to True"
                )
                self.quantize = True
            elif self.quantize and not quantization_already_active:
                logger.warning(
                    "GPTQ quantization is set to True without an "
                    "active quantization modifier."
                )
                self._build_quant_modifier()
            return  # use existing quantization modifier if there is one
        else:
            if not isinstance(self.quantize, Dict):
                raise ValueError(
                    "GPTQModifier.quantize accepts only a single "
                    "quantization modifier or a boolean. Found "
                    f"type {type(self.quantize)}"
                )
            if len(self.quantize) != 1:
                raise ValueError(
                    "GPTQModifier.quantize accepts only a single "
                    "quantization modifier or a boolean. Found "
                    f"{len(self.quantize)} modifiers"
                )
            if quantization_already_active:
                logger.warning(
                    "Attempting to initialize quantization for GPTQ "
                    "but a quantization modifier has already been applied. "
                    "The quantization configuration defined under the "
                    "GPTQ modifier will be ignored."
                )
                self.quantize = True
                return
            self._build_quant_modifier_from_dict(self.quantize)
            self.quantize = True

        if self._quantization_modifier:
            self._quantization_modifier.on_initialize_structure(state, **kwargs)

    def on_initialize(self, state: "State", **kwargs) -> bool:
        """
        Initialize and run the GPTQ algorithm on the current state

        :param state: session state storing input model and calibration data
        """
        if not self.initialized_structure_:
            self.on_initialize_structure(state, **kwargs)
        if self._quantization_modifier:
            self._quantization_modifier.initialize(state, **kwargs)
        if not self.quantize:
            raise ValueError("To use the GPTQModifier, quantization must be enabled.")
        
        if self.batch_size <= 0:
            batch_size = len(state.data.calib.dataset)
        else:
            batch_size = self.batch_size
        self._num_batches = math.ceil(len(state.data.calib.dataset) / batch_size)

        self.register_hooks(state.model)
        #torch.cuda.memory._record_memory_history(max_entries=1_000_000)
        try:
            self.calibration_forward(state.model, state.data.calib)
        finally:
            pass
            #torch.cuda.memory._dump_snapshot("bs10.pickle")
            #torch.cuda.memory._record_memory_history(enabled=None)
            #exit(0)

        self.remove_hooks()
        self.finish_compression(state.model)

        # freeze quantization
        state.model.apply(freeze_module_quantization)

        return True
    
    def finish_compression(self, model: torch.nn.Module):
        for module in model.modules():
            quant_args = getattr_chain(module, "quantization_scheme.weights", None)
            if quant_args is None:
                continue
            
            with align_module(module):

                if self.batch_size != -1:
                    weight = module.weight_acc / self._num_batches
                    delete_offload_parameter(module, "weight_acc")
                else:
                    weight = module.weight

                observer = Observer.load_from_registry(
                    quant_args.observer, quantization_args=quant_args
                )
                scale, zero_point = observer(weight)
                weight = fake_quantize(
                    weight,
                    scale,
                    zero_point,
                    quant_args,
                )
                update_offload_parameter(module, "weight", weight)
                update_offload_parameter(module, "weight_scale", scale)
                update_offload_parameter(module, "weight_zero_point", zero_point)

    def on_finalize(self, state: "State", **kwargs) -> bool:
        """
        disable the quantization observers used by the OBCQ algorithm

        :param state: session state storing input model and calibration data
        """
        if self._quantization_modifier:
            self._quantization_modifier.finalize(state, **kwargs)

        return True

    def calibration_forward(
        self, model: torch.nn.Module, dataloader: torch.utils.data.DataLoader
    ):
        """
        Perform calibration forward pass with one batch whose size is the size
        of the dataset

        :param model: model to perform forward pass with
        :param dataloader: dataloader containing calibration dataset
        """
        if self.batch_size <= 0:
            batch_size = len(dataloader.dataset)
        else:
            batch_size = self.batch_size
        dataloader = create_batch_dataloader(dataloader, batch_size=batch_size)
        with calibration_forward_context(model):
            run_calibration_forward(model, dataloader, mask_padding=True)

    def pre_compress_module(self, module: torch.nn.Module):
        if self.batch_size != -1:
            print("created aux buffers")
            register_offload_parameter(module, "weight_acc", torch.nn.Parameter(torch.zeros_like(module.weight.data), requires_grad=False))

    def compress_module(
        self,
        name: str,
        module: torch.nn.Module,
        args: Tuple[torch.Tensor, ...],
    ) -> float:
        """
        Quantize a module's weight according to the GPTQ algorithm

        :param name: name of module being quantized
        :param module: module being quantized
        :param args: input arguments for module forward pass

        :return: total loss from applying weight quantization to this module
        """
        logger.info(f"Quantizing {name}...")

        # Assume that first argument is the input
        inp = args[0]
        quant_args = getattr_chain(module, "quantization_scheme.weights")
        logger.info(f"Using {inp.size(0)} samples")

        with align_module(module):
            print(inp.shape)
            loss, quantized_weight, _scale, _zero_point, _g_idx = quantize_weight(
                module.weight.data,
                inp,
                quant_args,
                blocksize=self.block_size,
                percdamp=self.dampening_frac,
                module_class=type(module),
            )

            if self.batch_size != -1:
                module.weight_acc += quantized_weight
                update_offload_parameter(module, "weight_acc")
            else:
                module.weight -= module.weight
                module.weight += quantized_weight
                update_offload_parameter(module, "weight")

        return loss

    def _build_quant_modifier(self):
        """
        Build a quantization modifier based on the specified config_groups,
        ignore list, and num_calibration_steps.

        :postcondition: self._quantization_modifier is set to the built
            quantization modifier
        """

        quantization_args_names = [
            "config_groups",
            "targets",
            "scheme",
            "num_calibration_steps",
            "ignore",
            "disable_quantization_observer_epoch",
        ]

        quant_args = {
            key: getattr(self, key)
            for key in quantization_args_names
            if getattr(self, key, False)
        }

        logger.info(f"Building quantization modifier with args: {quant_args}")
        vllm_quant_config = {"QuantizationModifier": quant_args}
        self._build_quant_modifier_from_dict(vllm_quant_config)

    def _build_quant_modifier_from_dict(self, quant_config):
        modifier_type = list(quant_config.keys())[0]
        modifier_args = quant_config[modifier_type]
        self._quantization_modifier = ModifierFactory.create(
            modifier_type,
            allow_registered=True,
            allow_experimental=True,
            **modifier_args,
        )
