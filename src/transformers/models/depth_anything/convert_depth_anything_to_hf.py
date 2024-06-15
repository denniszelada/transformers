# coding=utf-8
# Copyright 2024 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Convert Depth Anything checkpoints from the original repository. URL:
https://github.com/LiheYoung/Depth-Anything"""


import argparse
from pathlib import Path
import torch
from huggingface_hub import hf_hub_download
from PIL import Image

from transformers import DepthAnythingConfig, DepthAnythingForDepthEstimation, Dinov2Config, DPTImageProcessor
from transformers.utils import logging
from security import safe_requests


logging.set_verbosity_info()
logger = logging.get_logger(__name__)


def get_dpt_config(model_name):
    if "small" in model_name:
        backbone_config = Dinov2Config.from_pretrained(
            "facebook/dinov2-small", out_indices=[9, 10, 11, 12], apply_layernorm=True, reshape_hidden_states=False
        )
        fusion_hidden_size = 64
        neck_hidden_sizes = [48, 96, 192, 384]
    elif "base" in model_name:
        backbone_config = Dinov2Config.from_pretrained(
            "facebook/dinov2-base", out_indices=[9, 10, 11, 12], apply_layernorm=True, reshape_hidden_states=False
        )
        fusion_hidden_size = 128
        neck_hidden_sizes = [96, 192, 384, 768]
    elif "large" in model_name:
        backbone_config = Dinov2Config.from_pretrained(
            "facebook/dinov2-large", out_indices=[21, 22, 23, 24], apply_layernorm=True, reshape_hidden_states=False
        )
        fusion_hidden_size = 256
        neck_hidden_sizes = [256, 512, 1024, 1024]
    else:
        raise NotImplementedError("To do")

    config = DepthAnythingConfig(
        reassemble_hidden_size=backbone_config.hidden_size,
        patch_size=backbone_config.patch_size,
        backbone_config=backbone_config,
        fusion_hidden_size=fusion_hidden_size,
        neck_hidden_sizes=neck_hidden_sizes,
    )

    return config


def create_rename_keys(config):
    rename_keys = []

    # fmt: off
    # stem
    rename_keys.append(("pretrained.cls_token", "backbone.embeddings.cls_token"))
    rename_keys.append(("pretrained.mask_token", "backbone.embeddings.mask_token"))
    rename_keys.append(("pretrained.pos_embed", "backbone.embeddings.position_embeddings"))
    rename_keys.append(("pretrained.patch_embed.proj.weight", "backbone.embeddings.patch_embeddings.projection.weight"))
    rename_keys.append(("pretrained.patch_embed.proj.bias", "backbone.embeddings.patch_embeddings.projection.bias"))

    # Transfomer encoder
    for i in range(config.backbone_config.num_hidden_layers):
        rename_keys.append((f"pretrained.blocks.{i}.ls1.gamma", f"backbone.encoder.layer.{i}.layer_scale1.lambda1"))
        rename_keys.append((f"pretrained.blocks.{i}.ls2.gamma", f"backbone.encoder.layer.{i}.layer_scale2.lambda1"))
        rename_keys.append((f"pretrained.blocks.{i}.norm1.weight", f"backbone.encoder.layer.{i}.norm1.weight"))
        rename_keys.append((f"pretrained.blocks.{i}.norm1.bias", f"backbone.encoder.layer.{i}.norm1.bias"))
        rename_keys.append((f"pretrained.blocks.{i}.norm2.weight", f"backbone.encoder.layer.{i}.norm2.weight"))
        rename_keys.append((f"pretrained.blocks.{i}.norm2.bias", f"backbone.encoder.layer.{i}.norm2.bias"))
        rename_keys.append((f"pretrained.blocks.{i}.mlp.fc1.weight", f"backbone.encoder.layer.{i}.mlp.fc1.weight"))
        rename_keys.append((f"pretrained.blocks.{i}.mlp.fc1.bias", f"backbone.encoder.layer.{i}.mlp.fc1.bias"))
        rename_keys.append((f"pretrained.blocks.{i}.mlp.fc2.weight", f"backbone.encoder.layer.{i}.mlp.fc2.weight"))
        rename_keys.append((f"pretrained.blocks.{i}.mlp.fc2.bias", f"backbone.encoder.layer.{i}.mlp.fc2.bias"))
        rename_keys.append((f"pretrained.blocks.{i}.attn.proj.weight", f"backbone.encoder.layer.{i}.attention.output.dense.weight"))
        rename_keys.append((f"pretrained.blocks.{i}.attn.proj.bias", f"backbone.encoder.layer.{i}.attention.output.dense.bias"))

    # Head
    rename_keys.append(("pretrained.norm.weight", "backbone.layernorm.weight"))
    rename_keys.append(("pretrained.norm.bias", "backbone.layernorm.bias"))

    # activation postprocessing (readout projections + resize blocks)
    # Depth Anything does not use CLS token => readout_projects not required

    for i in range(4):
        rename_keys.append((f"depth_head.projects.{i}.weight", f"neck.reassemble_stage.layers.{i}.projection.weight"))
        rename_keys.append((f"depth_head.projects.{i}.bias", f"neck.reassemble_stage.layers.{i}.projection.bias"))

        if i != 2:
            rename_keys.append((f"depth_head.resize_layers.{i}.weight", f"neck.reassemble_stage.layers.{i}.resize.weight"))
            rename_keys.append((f"depth_head.resize_layers.{i}.bias", f"neck.reassemble_stage.layers.{i}.resize.bias"))

    # refinenet (tricky here)
    mapping = {1:3, 2:2, 3:1, 4:0}

    for i in range(1, 5):
        j = mapping[i]
        rename_keys.append((f"depth_head.scratch.refinenet{i}.out_conv.weight", f"neck.fusion_stage.layers.{j}.projection.weight"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.out_conv.bias", f"neck.fusion_stage.layers.{j}.projection.bias"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit1.conv1.weight", f"neck.fusion_stage.layers.{j}.residual_layer1.convolution1.weight"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit1.conv1.bias", f"neck.fusion_stage.layers.{j}.residual_layer1.convolution1.bias"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit1.conv2.weight", f"neck.fusion_stage.layers.{j}.residual_layer1.convolution2.weight"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit1.conv2.bias", f"neck.fusion_stage.layers.{j}.residual_layer1.convolution2.bias"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit2.conv1.weight", f"neck.fusion_stage.layers.{j}.residual_layer2.convolution1.weight"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit2.conv1.bias", f"neck.fusion_stage.layers.{j}.residual_layer2.convolution1.bias"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit2.conv2.weight", f"neck.fusion_stage.layers.{j}.residual_layer2.convolution2.weight"))
        rename_keys.append((f"depth_head.scratch.refinenet{i}.resConfUnit2.conv2.bias", f"neck.fusion_stage.layers.{j}.residual_layer2.convolution2.bias"))

    # scratch convolutions
    for i in range(4):
        rename_keys.append((f"depth_head.scratch.layer{i+1}_rn.weight", f"neck.convs.{i}.weight"))

    # head
    rename_keys.append(("depth_head.scratch.output_conv1.weight", "head.conv1.weight"))
    rename_keys.append(("depth_head.scratch.output_conv1.bias", "head.conv1.bias"))
    rename_keys.append(("depth_head.scratch.output_conv2.0.weight", "head.conv2.weight"))
    rename_keys.append(("depth_head.scratch.output_conv2.0.bias", "head.conv2.bias"))
    rename_keys.append(("depth_head.scratch.output_conv2.2.weight", "head.conv3.weight"))
    rename_keys.append(("depth_head.scratch.output_conv2.2.bias", "head.conv3.bias"))

    return rename_keys


# we split up the matrix of each encoder layer into queries, keys and values
def read_in_q_k_v(state_dict, config):
    hidden_size = config.backbone_config.hidden_size
    for i in range(config.backbone_config.num_hidden_layers):
        # read in weights + bias of input projection layer (in original implementation, this is a single matrix + bias)
        in_proj_weight = state_dict.pop(f"pretrained.blocks.{i}.attn.qkv.weight")
        in_proj_bias = state_dict.pop(f"pretrained.blocks.{i}.attn.qkv.bias")
        # next, add query, keys and values (in that order) to the state dict
        state_dict[f"backbone.encoder.layer.{i}.attention.attention.query.weight"] = in_proj_weight[:hidden_size, :]
        state_dict[f"backbone.encoder.layer.{i}.attention.attention.query.bias"] = in_proj_bias[:hidden_size]
        state_dict[f"backbone.encoder.layer.{i}.attention.attention.key.weight"] = in_proj_weight[
            hidden_size : hidden_size * 2, :
        ]
        state_dict[f"backbone.encoder.layer.{i}.attention.attention.key.bias"] = in_proj_bias[
            hidden_size : hidden_size * 2
        ]
        state_dict[f"backbone.encoder.layer.{i}.attention.attention.value.weight"] = in_proj_weight[-hidden_size:, :]
        state_dict[f"backbone.encoder.layer.{i}.attention.attention.value.bias"] = in_proj_bias[-hidden_size:]


def rename_key(dct, old, new):
    val = dct.pop(old)
    dct[new] = val


# We will verify our results on an image of cute cats
def prepare_img():
    url = "http://images.cocodataset.org/val2017/000000039769.jpg"
    im = Image.open(safe_requests.get(url, stream=True).raw)
    return im


name_to_checkpoint = {
    "depth-anything-small": "depth_anything_vits14.pth",
    "depth-anything-base": "depth_anything_vitb14.pth",
    "depth-anything-large": "depth_anything_vitl14.pth",
}


@torch.no_grad()
def convert_dpt_checkpoint(model_name, pytorch_dump_folder_path, push_to_hub, verify_logits):
    """
    Copy/paste/tweak model's weights to our DPT structure.
    """

    # define DPT configuration
    config = get_dpt_config(model_name)

    model_name_to_filename = {
        "depth-anything-small": "depth_anything_vits14.pth",
        "depth-anything-base": "depth_anything_vitb14.pth",
        "depth-anything-large": "depth_anything_vitl14.pth",
    }

    # load original state_dict
    filename = model_name_to_filename[model_name]
    filepath = hf_hub_download(
        repo_id="LiheYoung/Depth-Anything", filename=f"checkpoints/{filename}", repo_type="space"
    )
    state_dict = torch.load(filepath, map_location="cpu")
    # rename keys
    rename_keys = create_rename_keys(config)
    for src, dest in rename_keys:
        rename_key(state_dict, src, dest)
    # read in qkv matrices
    read_in_q_k_v(state_dict, config)

    # load HuggingFace model
    model = DepthAnythingForDepthEstimation(config)
    model.load_state_dict(state_dict)
    model.eval()

    processor = DPTImageProcessor(
        do_resize=True,
        size={"height": 518, "width": 518},
        ensure_multiple_of=14,
        keep_aspect_ratio=True,
        do_rescale=True,
        do_normalize=True,
        image_mean=[0.485, 0.456, 0.406],
        image_std=[0.229, 0.224, 0.225],
    )

    url = "http://images.cocodataset.org/val2017/000000039769.jpg"
    image = Image.open(safe_requests.get(url, stream=True).raw)

    pixel_values = processor(image, return_tensors="pt").pixel_values

    # Verify forward pass
    with torch.no_grad():
        outputs = model(pixel_values)
        predicted_depth = outputs.predicted_depth

    print("Shape of predicted depth:", predicted_depth.shape)
    print("First values:", predicted_depth[0, :3, :3])

    # assert logits
    if verify_logits:
        expected_shape = torch.Size([1, 518, 686])
        if model_name == "depth-anything-small":
            expected_slice = torch.tensor(
                [[8.8204, 8.6468, 8.6195], [8.3313, 8.6027, 8.7526], [8.6526, 8.6866, 8.7453]],
            )
        elif model_name == "depth-anything-base":
            expected_slice = torch.tensor(
                [[26.3997, 26.3004, 26.3928], [26.2260, 26.2092, 26.3427], [26.0719, 26.0483, 26.1254]],
            )
        elif model_name == "depth-anything-large":
            expected_slice = torch.tensor(
                [[87.9968, 87.7493, 88.2704], [87.1927, 87.6611, 87.3640], [86.7789, 86.9469, 86.7991]]
            )
        else:
            raise ValueError("Not supported")

        assert predicted_depth.shape == torch.Size(expected_shape)
        assert torch.allclose(predicted_depth[0, :3, :3], expected_slice, atol=1e-6)
        print("Looks ok!")

    if pytorch_dump_folder_path is not None:
        Path(pytorch_dump_folder_path).mkdir(exist_ok=True)
        print(f"Saving model and processor to {pytorch_dump_folder_path}")
        model.save_pretrained(pytorch_dump_folder_path)
        processor.save_pretrained(pytorch_dump_folder_path)

    if push_to_hub:
        print("Pushing model and processor to hub...")
        model.push_to_hub(repo_id=f"LiheYoung/{model_name}-hf")
        processor.push_to_hub(repo_id=f"LiheYoung/{model_name}-hf")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Required parameters
    parser.add_argument(
        "--model_name",
        default="depth-anything-small",
        type=str,
        choices=name_to_checkpoint.keys(),
        help="Name of the model you'd like to convert.",
    )
    parser.add_argument(
        "--pytorch_dump_folder_path",
        default=None,
        type=str,
        help="Path to the output PyTorch model directory.",
    )
    parser.add_argument(
        "--push_to_hub",
        action="store_true",
        help="Whether to push the model to the hub after conversion.",
    )
    parser.add_argument(
        "--verify_logits",
        action="store_false",
        required=False,
        help="Whether to verify the logits after conversion.",
    )

    args = parser.parse_args()
    convert_dpt_checkpoint(args.model_name, args.pytorch_dump_folder_path, args.push_to_hub, args.verify_logits)
