from __future__ import annotations

import os
import glob
import json
import torch
import re
import shutil
import sys
from typing import Union

from transformers import AutoModelForCausalLM, GPTNeoForCausalLM, AutoTokenizer, LlamaTokenizer

import utils
import modeling.lazy_loader as lazy_loader
import koboldai_settings
from logger import logger, set_logger_verbosity

try:
    import breakmodel
except ModuleNotFoundError as e:
    # Breakmodel is only expected to work on GPU
    if not utils.koboldai_vars.use_colab_tpu:
        raise e

from modeling.inference_models.hf_torch import HFTorchInferenceModel
from modeling.tokenizer import GenericTokenizer

# 4-bit dependencies
from pathlib import Path
sys.path.insert(0, os.path.abspath(Path("repos/gptq")))
from gptj import load_quant as gptj_load_quant
from gptneox import load_quant as gptneox_load_quant
from llama import load_quant as llama_load_quant
from opt import load_quant as opt_load_quant
from offload import load_quant_offload
monkey_patched_4bit = False


def prepare_4bit_load(modelpath):
    path_4bit = os.path.join(modelpath, "model.safetensors")
    if os.path.isfile(path_4bit):
        return path_4bit, False

    path_4bit = os.path.join(modelpath, "model.ckpt")
    if os.path.isfile(path_4bit):
        return path_4bit, False

    # Legacy format support
    paths_4bit = ["4bit*.safetensors", "4bit*.pt"]
    paths_4bit_old = ["4bit-old.pt", "4bit-old.safetensors"]
    result = False
    groupsize = -1
    for p in paths_4bit:
        p = os.path.join(modelpath, p)
        val = [v for v in glob.glob(p) if "4bit-old" not in v]
        if val:
            result = val[0]
            fname = Path(result).parts[-1]
            g = re.findall("^(?:4bit)(?:-)(\\d+)(?:g-?)", fname)
            if g:
                groupsize = int(g[0])
            break

    global monkey_patched_4bit

    # Monkey-patch in old-format pt-file support
    if not result:
        print("4-bit file not found, falling back to old format.")
        for p in paths_4bit_old:
            p = os.path.join(modelpath, p)
            if os.path.isfile(p):
                result = p
                break

        if not result:
            print("4-bit old-format file not found, loading failed.")
            raise RuntimeError("4-bit load failed. PT/Safetensors-File not found.")

        import llama, opt, gptneox, gptj, old_quant
        llama.make_quant = old_quant.old_make_quant
        opt.make_quant = old_quant.old_make_quant
        gptneox.make_quant = old_quant.old_make_quant
        gptj.make_quant = old_quant.old_make_quant
        monkey_patched_4bit = True
    elif monkey_patched_4bit:
        # Undo monkey patch
        print("Undoing 4-bit old format monkey patch")
        import llama, opt, gptneox, gptj, quant
        llama.make_quant = quant.make_quant
        opt.make_quant = quant.make_quant
        gptneox.make_quant = quant.make_quant
        gptj.make_quant = quant.make_quant
        monkey_patched_4bit = False

    return result, groupsize


def load_model_gptq_settings():
    try:
        js   = json.loads(str(model.model_config).partition(' ')[2])
    except Exception as e:
        try:
            try:
                js = json.load(open(utils.koboldai_vars.custmodpth + "/config.json", "r"))
            except Exception as e:
                js = json.load(open(utils.koboldai_vars.custmodpth.replace('/', '_') + "/config.json", "r"))
        except Exception as e:
            utils.koboldai_vars.gptq_model = False
            return

    gptq_legacy_files = glob.glob(os.path.join(utils.koboldai_vars.custmodpth, "4bit*.pt")) + glob.glob(os.path.join(utils.koboldai_vars.custmodpth, "4bit*.safetensors"))
    if "gptq_bits" in js:
        utils.koboldai_vars.gptq_model = True
        utils.koboldai_vars.gptq_bits = js["gptq_bits"]
        utils.koboldai_vars.gptq_groupsize = js.get("gptq_groupsize", -1)
        safetensors_file = os.path.join(utils.koboldai_vars.custmodpth, "model.safetensors")
        pt_file = os.path.join(utils.koboldai_vars.custmodpth, "model.ckpt")
        utils.koboldai_vars.gptq_file = safetensors_file if os.path.isfile(safetensors_file) else pt_file
    elif gptq_legacy_files:
        utils.koboldai_vars.gptq_model = True
        utils.koboldai_vars.gptq_bits = 4
        utils.koboldai_vars.gptq_file = gptq_legacy_files[0]
        fname = Path(utils.koboldai_vars.gptq_file).parts[-1]
        g = re.findall("^(?:4bit)(?:-)(\\d+)(?:g-?)", fname)
        utils.koboldai_vars.gptq_groupsize = int(g[0]) if g else -1
    else:
        utils.koboldai_vars.gptq_model = False


class HFTorch4BitInferenceModel(HFTorchInferenceModel):
    def _load(self, save_model: bool, initial_load: bool) -> None:
        utils.koboldai_vars.allowsp = True

        # Make model path the same as the model name to make this consistent
        # with the other loading method if it isn't a known model type. This
        # code is not just a workaround for below, it is also used to make the
        # behavior consistent with other loading methods - Henk717
        # if utils.koboldai_vars.model not in ["NeoCustom", "GPT2Custom"]:
        #     utils.koboldai_vars.custmodpth = utils.koboldai_vars.model

        if self.model_name == "NeoCustom":
            self.model_name = os.path.basename(
                os.path.normpath(utils.koboldai_vars.custmodpth)
            )
            utils.koboldai_vars.model = self.model_name

        self.init_model_config()

        gpulayers = utils.args.breakmodel_gpulayers

        try:
            self.gpu_layers_list = [int(l) for l in gpulayers.split(",")]
        except ValueError:
            self.gpu_layers_list = [utils.num_layers(self.model_config)]
        self.offload_4bit = sum(self.gpu_layers_list) < utils.num_layers(self.model_config)

        if self.offload_4bit:
            utils.koboldai_vars.lazy_load = False
            print("4-bit CPU offloader active")

        tf_kwargs = {
            "low_cpu_mem_usage": True,
        }

        # If we're using torch_lazy_loader, we need to get breakmodel config
        # early so that it knows where to load the individual model tensors
        if (
            self.lazy_load
            and utils.koboldai_vars.hascuda
            and utils.koboldai_vars.breakmodel
            and not utils.koboldai_vars.nobreakmodel
        ):
            self.breakmodel_device_config(self.model_config)

        if self.lazy_load:
            # If we're using lazy loader, we need to figure out what the model's hidden layers are called
            with lazy_loader.use_lazy_load(
                dematerialized_modules=True, use_accelerate_init_empty_weights=True
            ):
                try:
                    metamodel = AutoModelForCausalLM.from_config(self.model_config)
                    utils.layers_module_names = utils.get_layers_module_names(metamodel)
                    utils.module_names = list(metamodel.state_dict().keys())
                    utils.named_buffers = list(metamodel.named_buffers(recurse=True))
                except Exception as e:
                    logger.warning(f"Gave up on lazy loading due to {e}")
                    self.lazy_load = False

        # Download model from Huggingface if it does not exist, otherwise load locally
        with self._maybe_use_float16(), lazy_loader.use_lazy_load(
            enable=self.lazy_load,
            callback=self._get_lazy_load_callback(utils.num_layers(self.model_config))
            if self.lazy_load
            else None,
            dematerialized_modules=True,
        ):
            if self.lazy_load:
                # torch_lazy_loader.py and low_cpu_mem_usage can't be used at the same time
                tf_kwargs.pop("low_cpu_mem_usage", None)

            if self.get_local_model_path():
                # Model is stored locally, load it.
                self.model = self._get_model(self.get_local_model_path(), tf_kwargs)
                self.tokenizer = self._get_tokenizer(self.get_local_model_path())
            else:
                # Model not stored locally, we need to download it.

                # _rebuild_tensor patch for casting dtype and supporting LazyTensors
                old_rebuild_tensor = torch._utils._rebuild_tensor

                def new_rebuild_tensor(
                    storage: Union[lazy_loader.LazyTensor, torch.Storage],
                    storage_offset,
                    shape,
                    stride,
                ):
                    if not isinstance(storage, lazy_loader.LazyTensor):
                        dtype = storage.dtype
                    else:
                        dtype = storage.storage_type.dtype
                        if not isinstance(dtype, torch.dtype):
                            dtype = storage.storage_type(0).dtype
                    if dtype is torch.float32 and len(shape) >= 2:
                        utils.koboldai_vars.fp32_model = True
                    return old_rebuild_tensor(storage, storage_offset, shape, stride)

                torch._utils._rebuild_tensor = new_rebuild_tensor
                self.model = self._get_model(self.model_name, tf_kwargs)
                self.tokenizer = self._get_tokenizer(self.model_name)
                torch._utils._rebuild_tensor = old_rebuild_tensor

                if save_model:
                    self.tokenizer.save_pretrained(
                        self.get_local_model_path(ignore_existance=True)
                    )

                    if utils.koboldai_vars.fp32_model and not breakmodel.disk_blocks:
                        # Use save_pretrained to convert fp32 models to fp16,
                        # unless we are using disk cache because save_pretrained
                        # is not supported in that case
                        self.model = self.model.half()
                        self.model.save_pretrained(
                            self.get_local_model_path(ignore_existance=True),
                            max_shard_size="500MiB",
                        )

                    else:
                        # For fp16 models, we can just copy the model files directly
                        import transformers.configuration_utils
                        import transformers.modeling_utils
                        import transformers.file_utils
                        import huggingface_hub

                        # Save the config.json
                        shutil.move(
                            os.path.realpath(
                                huggingface_hub.hf_hub_download(
                                    self.model_name,
                                    transformers.configuration_utils.CONFIG_NAME,
                                    revision=utils.koboldai_vars.revision,
                                    cache_dir="cache",
                                    local_files_only=True,
                                    legacy_cache_layout=False,
                                )
                            ),
                            os.path.join(
                                self.get_local_model_path(ignore_existance=True),
                                transformers.configuration_utils.CONFIG_NAME,
                            ),
                        )

                        if utils.num_shards is None:
                            # Save the pytorch_model.bin or model.safetensors of an unsharded model
                            any_success = False
                            possible_checkpoint_names = [
                                transformers.modeling_utils.WEIGHTS_NAME,
                                "model.safetensors",
                            ]

                            for possible_checkpoint_name in possible_checkpoint_names:
                                try:
                                    shutil.move(
                                        os.path.realpath(
                                            huggingface_hub.hf_hub_download(
                                                self.model_name,
                                                possible_checkpoint_name,
                                                revision=utils.koboldai_vars.revision,
                                                cache_dir="cache",
                                                local_files_only=True,
                                                legacy_cache_layout=False,
                                            )
                                        ),
                                        os.path.join(
                                            self.get_local_model_path(
                                                ignore_existance=True
                                            ),
                                            possible_checkpoint_name,
                                        ),
                                    )
                                    any_success = True
                                except Exception:
                                    pass

                            if not any_success:
                                raise RuntimeError(f"Couldn't find any of {possible_checkpoint_names} in cache for {self.model_name} @ '{utils.koboldai_vars.revisison}'")
                        else:
                            # Handle saving sharded models

                            with open(utils.from_pretrained_index_filename) as f:
                                map_data = json.load(f)
                            filenames = set(map_data["weight_map"].values())
                            # Save the pytorch_model.bin.index.json of a sharded model
                            shutil.move(
                                os.path.realpath(utils.from_pretrained_index_filename),
                                os.path.join(
                                    self.get_local_model_path(ignore_existance=True),
                                    transformers.modeling_utils.WEIGHTS_INDEX_NAME,
                                ),
                            )
                            # Then save the pytorch_model-#####-of-#####.bin files
                            for filename in filenames:
                                shutil.move(
                                    os.path.realpath(
                                        huggingface_hub.hf_hub_download(
                                            self.model_name,
                                            filename,
                                            revision=utils.koboldai_vars.revision,
                                            cache_dir="cache",
                                            local_files_only=True,
                                            legacy_cache_layout=False,
                                        )
                                    ),
                                    os.path.join(
                                        self.get_local_model_path(
                                            ignore_existance=True
                                        ),
                                        filename,
                                    ),
                                )
                    shutil.rmtree("cache/")

        if not self.lazy_load:
            utils.layers_module_names = utils.get_layers_module_names(self.model)
            utils.module_names = list(self.model.state_dict().keys())
            utils.named_buffers = list(self.model.named_buffers(recurse=True))

        if (
            utils.koboldai_vars.badwordsids is koboldai_settings.badwordsids_default
            and utils.koboldai_vars.model_type not in ("gpt2", "gpt_neo", "gptj")
        ):
            utils.koboldai_vars.badwordsids = [
                [v]
                for k, v in self.tokenizer.get_vocab().items()
                if any(c in str(k) for c in "[]")
            ]

        self.patch_embedding()

        if not self.offload_4bit:
            self.model = self.model.half().to(utils.koboldai_vars.gpu_device)

        self.model.kai_model = self
        utils.koboldai_vars.modeldim = self.get_hidden_size()

    def _get_model(self, location: str, tf_kwargs: Dict):
        if not utils.koboldai_vars.custmodpth:
            pass
        groupsize = utils.koboldai_vars.gptq_groupsize

        path_4bit, legacy_groupsize = prepare_4bit_load(utils.koboldai_vars.custmodpth)

        if legacy_groupsize is not False:
            groupsize = legacy_groupsize

        print(f"Using 4-bit file: {path_4bit}, groupsize {groupsize}")

        print(f"Trying to load {utils.koboldai_vars.model_type} model in 4-bit")
        if utils.koboldai_vars.model_type == "gptj":
            model = load_quant_offload(gptj_load_quant, utils.koboldai_vars.custmodpth, path_4bit, 4, groupsize, self.gpu_layers_list)
        elif utils.koboldai_vars.model_type == "gpt_neox":
            model = load_quant_offload(gptneox_load_quant, utils.koboldai_vars.custmodpth, path_4bit, 4, groupsize, self.gpu_layers_list)
        elif utils.koboldai_vars.model_type == "llama":
            model = load_quant_offload(llama_load_quant, utils.koboldai_vars.custmodpth, path_4bit, 4, groupsize, self.gpu_layers_list)
        elif utils.koboldai_vars.model_type == "opt":
            model = load_quant_offload(opt_load_quant, utils.koboldai_vars.custmodpth, path_4bit, 4, groupsize, self.gpu_layers_list)
        else:
            raise RuntimeError(f"4-bit load failed. Model type {utils.koboldai_vars.model_type} not supported in 4-bit")

        return model.half() if not self.offload_4bit else model

    def _get_tokenizer(self, location: str):
        if utils.koboldai_vars.model_type == "llama":
            tokenizer = LlamaTokenizer.from_pretrained(utils.koboldai_vars.custmodpth)
        else:
            tokenizer = AutoTokenizer.from_pretrained(utils.koboldai_vars.custmodpth)

        return GenericTokenizer(tokenizer)
