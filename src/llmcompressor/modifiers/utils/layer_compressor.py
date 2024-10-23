import contextlib
import operator
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch
from compressed_tensors import get_execution_device
from loguru import logger
from tqdm import tqdm

from llmcompressor.modifiers.utils.compression_wrapper import ModuleCompressionWrapper
from llmcompressor.modifiers.utils.pytorch_helpers import EarlyStopException
from llmcompressor.pytorch.utils import tensors_to_device
from llmcompressor.utils.fsdp.context import (
    fix_fsdp_module_name,
    summon_full_params_context,
)
from llmcompressor.utils.helpers import getattr_chain
from llmcompressor.utils.metric_logging import CompressionLogger
from llmcompressor.utils.pytorch.module import (
    get_layers,
    get_no_split_params,
    get_prunable_layers,
    set_layer,
)

__all__ = ["SequentialLayerCompressor", "LayerCompressor"]


class HooksMixin:
    HOOKS_DISABLED: bool = False

    @classmethod
    def hook(cls, func):
        def wrapped(*args, **kwargs):
            if cls.HOOKS_DISABLED:
                return

            func(*args, **kwargs)

        return wrapped

    @classmethod
    @contextlib.contextmanager
    def disable_hooks(cls):
        try:
            cls.HOOKS_DISABLED = True
            yield
        finally:
            cls.HOOKS_DISABLED = False

    def __init__(self):
        self._hooks = []

    def register_hook(self, handle: torch.utils.hooks.RemovableHandle):
        self._hooks.append(handle)

    def remove_hooks(self):
        for hook in self._hooks:
            hook.remove()


class SequentialLayerCompressor(HooksMixin):
    """
    Apply a given compression function to a model during the model's calibration
    forward pass

    Lifecycle:
        - QuantizationModifier.initialize(model)
        - SequentialLayerCompressor(compress_fn)
        - register_hooks(model)
        - model.forward()
            - compress_fn(name, target_module, args)
        - remove_hooks()

    :param compress_fn: Function to be called on target modules
    :param true_sequential: Used to control the granularity of compression updates
        through the forward pass. Set to True to use the weight-compressed outputs
        of each module, set to False to use the weight-compressed outputs of each
        layer (transformer block), defaults to False
    """

    def __init__(
        self,
        compress_fn: Callable[[str, torch.nn.Module, torch.Tensor], float],
        true_sequential: bool = False,
    ):
        HooksMixin.__init__(self)
        self.compress_fn = compress_fn
        self.true_sequential = true_sequential

        self._layer_index = 0
        self._num_layers = 0

    def register_hooks(
        self,
        model: torch.nn.Module,
        sequential_targets: Optional[Union[str, List[str]]] = None,
    ):
        # find layers (used for printing even if true_sequential=True)
        # if no targets are provided, default to the modules that shouldn't be
        # split by FSDP. For Transformers models this is equivalent to the
        # decoder layers (ie LlamaDecoderLayer)
        if sequential_targets is None:
            sequential_targets = get_no_split_params(model)
        layers = get_layers(sequential_targets, model)
        self._num_layers = len(layers)

        for name, module in model.named_modules():
            if getattr_chain(module, "quantization_scheme.weights", None) is not None:
                pre_hook = partial(self.target_pre_forward, name)
                post_hook = partial(self.target_post_forward, name)
                self.register_hook(module.register_forward_pre_hook(pre_hook))
                self.register_hook(module.register_forward_hook(post_hook))

            if name in layers.keys():
                pre_hook = partial(self.layer_pre_forward, name)
                post_hook = partial(self.layer_post_forward, name)
                self.register_hook(module.register_forward_pre_hook(pre_hook))
                self.register_hook(
                    module.register_forward_hook(post_hook, with_kwargs=True)
                )

    @HooksMixin.hook
    def target_pre_forward(
        self, name: str, module: torch.nn.Module, args: Tuple[Any, ...]
    ):
        if self.true_sequential:
            # compress first so output is from compressed weights
            with CompressionLogger(module) as comp_logger:
                loss = self.compress_fn(name, module, args)
                comp_logger.set_loss(loss)

    @HooksMixin.hook
    def target_post_forward(
        self,
        name: str,
        module: torch.nn.Module,
        args: Tuple[Any, ...],
        _output: Tuple[Any, ...],
    ):
        if not self.true_sequential:
            # compress after so output is from uncompressed weights
            with CompressionLogger(module) as comp_logger:
                loss = self.compress_fn(name, module, args)
                comp_logger.set_loss(loss)

    @HooksMixin.hook
    def layer_pre_forward(self, _name: str, _module: torch.nn.Module, _args: Any):
        logger.info(
            f"\n===== Compressing layer {self._layer_index}/{self._num_layers} ====="
        )

    @HooksMixin.hook
    def layer_post_forward(
        self,
        name: str,
        module: torch.nn.Module,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        output: Tuple[Any, ...],
    ):
        if not self.true_sequential:
            # rerun with (now) compressed weights
            with HooksMixin.disable_hooks():
                compressed_output = module(*args, **kwargs)

            error = torch.nn.functional.l1_loss(output[0], compressed_output[0])
            logger.info(f"Mean output error from quantization: {error:.3f}")

        self._layer_index += 1
        return output


class LayerCompressor:
    """
    Runs weight sparisification on a single layer using calibration data inputs. The
    layer may contain submodules. The specific sparsification algorithm is determined
    by module_compressor_class.

    Lifecycle:
        - pre_compress()
            - compressible_modules()
            - module_compressor_class.register_forward_hook()
        - compress()
            - module_compressor_class.compress()
        - post_compress()
        - revert_layer_wrappers()

    :param module_compressor_class: wrapper class to use for root modules
    :param model: model containing the layer we are running compression on
    :param layer: layer to run compression on
    :param layer_index: index of layer in the model
    :param args: additional keyword arguments
    """

    def __init__(
        self,
        module_compressor_class: ModuleCompressionWrapper,
        model: torch.nn.Module,
        layer: torch.nn.Module,
        layer_index: int,
        name: str,
        args: Dict,
    ):
        self.module_compressor_class = module_compressor_class
        self.model = model
        self.layer = layer
        self.layer_index = layer_index
        self.name = name
        self.args = args
        self.handles = []
        self.early_stop_handle = None
        self.modules = {}

    def compressible_modules(self) -> Dict:
        """
        Get the list of modules in the layer that can be compressed

        :return: dictionary of compressible modules
        """
        compressible_layers = get_prunable_layers(self.layer)
        return compressible_layers

    def set_early_stop(self):
        """
        Adds an early stopping exception to the input of the layer. This will cause the
        model to immediately exit the forward pass when reaching this layer.
        """

        def trigger_early_stop_fn(self, args, kwargs):
            raise EarlyStopException(args, kwargs)

        self.early_stop_handle = self.layer.register_forward_pre_hook(
            trigger_early_stop_fn, with_kwargs=True
        )

    def clear_early_stop(self):
        """
        Clears the early stopping handle
        """
        if self.early_stop_handle is not None:
            self.early_stop_handle.remove()
            self.early_stop_handle = None

    def pre_compress(self):
        """
        Sets up the CompressionWrapper objects for each compressible module, adding a
        hook for computing the Hessians as calibration data is passed through.
        """
        subset = self.compressible_modules()

        for name in subset:
            layer = subset[name]
            full_name = self._get_full_submodule_name(name)
            with summon_full_params_context(self.layer):
                wrapper = self.module_compressor_class(full_name, layer)
            if len(name) == 0:  # special case if layer has no children (i.e. lm_head)
                with summon_full_params_context(self.model):
                    set_layer(full_name, wrapper, self.model)
            else:
                set_layer(name, wrapper, self.layer)
            self.modules[name] = wrapper

        self.layer = operator.attrgetter(self.name)(self.model)

        def add_batch(name):
            def tmp(_, inp, out):
                self.modules[name].add_batch(inp[0].data, out.data)

            return tmp

        for name in self.modules:
            self.handles.append(subset[name].register_forward_hook(add_batch(name)))

    def calibrate_layer(self, intermediates: Tuple[Tuple, Dict]) -> Tuple[Tuple, Dict]:
        """
        Runs all calibration samples through the stored layer

        :param intermediates: inputs to run through the layer
        :return: outputs of the layer
        """
        outputs = [None for _ in range(len(intermediates))]
        for idx in tqdm(range(len(intermediates))):
            args, kwargs = intermediates[idx]
            device = get_execution_device(self.layer)
            output = self.layer(*tensors_to_device(args, device), **kwargs)
            outputs[idx] = (tensors_to_device(output, "cpu"), kwargs)
            torch.cuda.empty_cache()

        return outputs

    def post_compress(self):
        """
        remove the add_batch forward hooks after compression is complete
        """
        for handle in self.handles:
            handle.remove()

        self.handles = []

    def revert_layer_wrappers(self):
        """
        Reverts wrapped root modules back to their original structure
        """
        for name, module_wrapper in self.modules.items():
            full_name = self._get_full_submodule_name(name)
            if len(name) == 0:  # special case if layer has no children (i.e. lm_head)
                with summon_full_params_context(self.model):
                    set_layer(full_name, module_wrapper.layer, self.model)
            else:
                set_layer(name, module_wrapper.layer, self.layer)
            torch.cuda.empty_cache()
        self.modules = None

    def compress(self):
        """
        Apply compression to each wrapped submodule in the layer
        """

        @torch.no_grad()
        def compress_module(module):
            if isinstance(module, self.module_compressor_class):
                full_name = self._get_full_submodule_name(module.name)
                logger.info(f"Compressing {full_name}...")
                module.compress(**self.args)
                module.free()

        self.layer.apply(compress_module)
        torch.cuda.empty_cache()

    def _get_full_submodule_name(self, name):
        full_name = ".".join(x for x in [self.name, name] if len(x) > 0)
        full_name = fix_fsdp_module_name(full_name)
        return full_name
