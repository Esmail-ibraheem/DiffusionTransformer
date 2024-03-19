"""
Created on: 2024, March 11th
Author: 'Cecover' on Github

Title: Stable Diffusion using Transformer for U-net replacement
Framework used: PyTorch
Code reference: https://github.com/facebookresearch/DiT/blob/main/models.py

Difference between this and referenced code:

1. Written from scratch
2. Optimized for single GPU training
3. Added more details (mainly the expected tensor shapes)
"""

import math
import numpy as np
import collections.abc

import torch
import torch.nn as nn
import torch.nn.functional as F

from typing import Optional, Tuple
from itertools import repeat


def modulate(tensor, shift, scale):
    return tensor * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)


def n_tuples(n):
    def parse(x):
        if isinstance(x, collections.abc.Iterable) and not isinstance(x, str):
            return tuple(x)
        return tuple(repeat(x, n))

    return parse


to_2tuple = n_tuples(2)


# ===== Embedding Layers =====
class TimestepEmbedder(nn.Module):
    """
    Embeds scalar timestep into a vector representation
    """

    def __init__(
        self,
        timestep_hidden_size: int,
        frequency_embedding_size: int,
        perceptron_bias: bool,
    ):
        super().__init__()

        self.frequency_embedding_size = frequency_embedding_size

        self.perceptron = nn.Sequential(
            nn.Linear(
                frequency_embedding_size, timestep_hidden_size, bias=perceptron_bias
            ),
            nn.SiLU(),
            nn.Linear(timestep_hidden_size, timestep_hidden_size, bias=perceptron_bias),
        )

    @staticmethod
    def time_stepper(
        tensor: torch.Tensor, embedding_size: int, max_period: int = 10000
    ):
        half = embedding_size // 2
        frequency = torch.exp(
            -math.log(max_period)
            * torch.arange(start=0, end=half, dtype=torch.float32)
            / half
        )
        frequency.to(device=tensor.device)  # MUST
        args = tensor[:, None].float() * frequency[None]
        embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)

        if embedding_size % 2:
            embedding = torch.cat(
                [embedding, torch.zeros_like(embedding[:, :1])], dim=-1
            )

        return embedding

    def forward(self, tensor: torch.Tensor) -> torch.Tensor:
        t_frequency = self.time_stepper(tensor, self.frequency_embedding_size)
        t_embedding = self.perceptron(t_frequency)

        """
        Expected input shape: [tensor, ]
        Expected output shape: [tensor, hidden_size]
        Assuming tensor has shape of (48, ) and hidden size of 1152, the size will be [48, 1152]
        """

        return t_embedding


# ===== Label Embedding =====
class LabelEmbedder(nn.Module):
    """
    Embeds class labels into vector representation.
    Code also handles label dropout for classifier-free guidance
    """

    def __init__(self, num_classes: int, hidden_size: int, dropout_prob: float):
        super().__init__()

        use_config_embedding = dropout_prob > 0

        self.num_classes = num_classes
        self.dropout_prob = dropout_prob
        self.embedding_table = nn.Embedding(
            num_classes + use_config_embedding, hidden_size
        )

    def token_dropper(self, labels: torch.Tensor):
        """
        Drops labels to enable classifier-free guidance
        """

        drop_id = torch.rand(labels.shape[0], device=labels.device) < self.dropout_prob
        new_labels = torch.where(drop_id, self.num_classes, labels)

        return new_labels

    def forward(self, labels: torch.Tensor):
        use_dropout = self.dropout_prob > 0

        if use_dropout:
            labels = self.token_dropper(labels)

        embeddings = self.embedding_table(labels)

        """
        Expected input shape: [tensor, ]
        Expected output shape: [tensor, hidden_size]
        Assuming tensor has shape of (48, ) and hidden size of 1152, the size will be [48, 1152]
        """

        return embeddings


# ===== Core Diffusion Transformer Model =====
class Attention(nn.Module):
    """
    Modularized Attention mechanism, so it is easier to be modified on later uses

    Implementation: Multi-head scaled dot product attention - Vaswani et al. in 2017
    Code reference: https://github.com/sooftware/attentions/blob/master/attentions.py
    """

    def __init__(self, attention_dimension: int, attention_heads: int):
        super().__init__()

        assert (
            attention_dimension % attention_heads == 0
        ), "Attention dimension and head size must be divisible!"

        self.num_heads = attention_heads
        self.div_dimension = int(attention_dimension / attention_heads)

        self.query_projection = nn.Linear(
            attention_dimension, self.num_heads * self.div_dimension
        )
        self.key_projection = nn.Linear(
            attention_dimension, self.num_heads * self.div_dimension
        )
        self.value_projection = nn.Linear(
            attention_dimension, self.num_heads * self.div_dimension
        )

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        batch_size = value.size(0)

        query = self.query_projection(query).view(
            batch_size, -1, self.num_heads, self.div_dimension
        )  # B * Q_len * H * D
        key = self.query_projection(key).view(
            batch_size, -1, self.num_heads, self.div_dimension
        )  # B * K_len * H * D
        value = self.query_projection(value).view(
            batch_size, -1, self.num_heads, self.div_dimension
        )  # B * V_len * H * D

        query = (
            query.permute(2, 0, 1, 3)
            .contiguous()
            .view(batch_size * self.num_heads, -1, self.div_dimension)
        )  # (B*N) * Q_len * D
        key = (
            key.permute(2, 0, 1, 3)
            .contiguous()
            .view(batch_size * self.num_heads, -1, self.div_dimension)
        )  # (B*N) * K_len * D
        value = (
            value.permute(2, 0, 1, 3)
            .contiguous()
            .view(batch_size * self.num_heads, -1, self.div_dimension)
        )  # (B*N) * V_len * D

        attention_score = torch.bmm(query, key.transpose(1, 2)) / np.sqrt(
            self.div_dimension
        )

        if mask is not None:
            mask = mask.unsqueeze(1).repeat(
                1, self.num_heads, 1, 1
            )  # B * N * Q_len * K_len
            attention_score.masked_fill_(
                mask.view(attention_score.size()), -float("Inf")
            )

        attention_matrix = F.softmax(attention_score, dim=-1)
        context = torch.bmm(attention_matrix, value)
        context = context.view(self.num_heads, batch_size, -1, self.div_dimension)
        context = (
            context.permute(1, 2, 0, 3)
            .contiguous()
            .view(batch_size, -1, self.num_heads * self.div_dimension)
        )  # B * T * (N*D)

        """
        Expected input shape: [batch size, token size, hidden dimension]
        Expected output shape: [batch size, token size, hidden dimension]
        
        Output of this needs to have [0] on the back since it was a tuple object. 
        [0] returns the actual end value
        [1] returns the attention matrix
        """

        return context, attention_matrix


class AttentionPerceptron(nn.Module):
    """
    This needs to be separated since there is research regarding better alternatives to the usual MLP

    Follows this strongly: https://github.com/huggingface/pytorch-image-models/blob/main/timm/layers/mlp.py
    """

    def __init__(
        self,
        in_features: int,
        hidden_features: Optional[int],
        out_features: Optional[int],
        layer_norm: bool,
        bias: bool,
        dropout_prob: float,
    ):
        super().__init__()

        out_features = in_features if out_features is None else out_features
        hidden_features = in_features if hidden_features is None else hidden_features
        bias = to_2tuple(bias)
        drop_probs = to_2tuple(dropout_prob)

        self.fc1 = nn.Linear(in_features, hidden_features, bias=bias[0])
        self.activation = nn.GELU(approximate="tanh")
        self.drop1 = nn.Dropout(drop_probs[0])
        self.norm = (
            nn.LayerNorm(hidden_features) if layer_norm is True else nn.Identity()
        )
        self.fc2 = nn.Linear(hidden_features, out_features, bias=bias[1])
        self.drop2 = nn.Dropout(drop_probs[1])

    def forward(self, x):
        x = self.fc1(x)
        x = self.activation(x)
        x = self.drop1(x)
        x = self.norm(x)
        x = self.fc2(x)
        x = self.drop2(x)

        """
        Expected input shape: [batch size, token size, hidden dimension]
        Expected output shape: [batch size, token size, hidden dimension]
        """

        return x


class DiffusionTransformer(nn.Module):
    """
    A transformer block for diffusion models using adaptive layer norm zero (adaLN-Zero) conditioning
    """

    def __init__(
        self,
        attention_embedding_size: int,
        attention_heads: int,
        hidden_size: int,
        mlp_ratio: float,
        layernorm_affine: bool,
        layernorm_epsilon: float,
        perceptron_dropout_rate: float,
        perceptron_bias: bool,
        perceptron_layernorm: bool,
    ):
        super().__init__()

        self.norm1 = nn.LayerNorm(
            hidden_size, elementwise_affine=layernorm_affine, eps=layernorm_epsilon
        )
        self.norm2 = nn.LayerNorm(
            hidden_size, elementwise_affine=layernorm_affine, eps=layernorm_epsilon
        )

        self.attention = Attention(attention_embedding_size, attention_heads)

        self.perceptron_hidden_dim = int(hidden_size * mlp_ratio)
        self.perceptron = AttentionPerceptron(
            in_features=hidden_size,
            hidden_features=self.perceptron_hidden_dim,
            out_features=hidden_size,
            dropout_prob=perceptron_dropout_rate,
            bias=perceptron_bias,
            layer_norm=perceptron_layernorm,
        )

        self.adaptiveLN = nn.Sequential(
            nn.SiLU(), nn.Linear(hidden_size, 6 * hidden_size, bias=True)
        )

    def forward(self, x, c):
        attn_shift, attn_scale, attn_gate, mlp_shift, mlp_scale, mlp_gate = (
            self.adaptiveLN(c).chunk(6, dim=1)
        )
        attn_input = modulate(self.norm1(x), attn_shift, attn_scale)
        attn_out = (
            x
            + attn_gate.unsqueeze(1)
            * self.attention(query=attn_input, key=attn_input, value=attn_input)[0]
        )  # This is a tuple
        perceptron_input = modulate(self.norm2(attn_out), mlp_shift, mlp_scale)
        x = x + mlp_gate.unsqueeze(1) * self.perceptron(perceptron_input)

        """
        Expected x shape: [batch size, token size, hidden dimension]
        Expected c shape: [tensor, hidden_size] (please refer to LabelEmbedding and TimestepEmbedder)
        Expected output shape: [batch size, token size, hidden dimension]
        """

        return x


class FinalLayer(nn.Module):
    """
    The final DiT layer
    """

    def __init__(self, hidden_size: int, patch_size: int, out_channels: int):
        super().__init__()
        self.layernorm = nn.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6)
        self.linear = nn.Linear(
            hidden_size, patch_size * patch_size * out_channels, bias=True
        )
        self.AdaptiveLN = nn.Sequential(
            nn.SiLU(), nn.Linear(hidden_size, 2 * hidden_size, bias=True)
        )

    def forward(self, x, c):
        shift, scale = self.AdaptiveLN(c).chunk(2, dim=1)
        x = modulate(self.layernorm(x), shift, scale)
        x = self.linear(x)

        """
        Expected x shape: [batch size, token size, hidden dimension]
        Expected c shape: [tensor, hidden_size] (please refer to LabelEmbedding and TimestepEmbedder)
        Expected output shape: [batch size, token size, hidden dimension]
        """

        return x


# ===== Timestep embedder tester =====
x = torch.rand((3,))
hidden_size = 128
frequency_size = 10000
bias = True

# Function, init, and output
t_embedder = TimestepEmbedder(hidden_size, frequency_size, bias)
nn.init.normal_(t_embedder.perceptron[0].weight, std=0.02)
nn.init.normal_(t_embedder.perceptron[2].weight, std=0.02)
print("Timestep Embedder final shape: ", t_embedder(x).shape)

t = t_embedder(x)

# ===== Label embedder tester =====
y = torch.randint(low=1, high=999, size=(3,))
num_classes = 1000
dropout_prob = 0.1

# Function, init, and output
l_embedder = LabelEmbedder(num_classes, hidden_size, dropout_prob)
nn.init.normal_(l_embedder.embedding_table.weight, std=0.02)
print("Label Embedder final shape: ", l_embedder(y).shape)

z = l_embedder(y)

# ===== Attention tester =====
query = torch.rand((3, 784, 128))
key = torch.rand((3, 784, 128))
value = torch.rand((3, 784, 128))
mask = torch.ones((3, 16, 16)).bool()

attention_embedding_size = 128
attention_heads = 2

attention = Attention(attention_embedding_size, attention_heads)
context, attention_matrix = attention(query, key, value, mask=None)
print("Context final shape: ", context.shape)
print("Attention matrix final shape: ", attention_matrix.shape)

# Regular comparison
basic_attention = nn.MultiheadAttention(
    embed_dim=attention_embedding_size, num_heads=attention_heads
)
basic_context, basic_weights = basic_attention(query, key, value)
print("Basic attention final shape: ", basic_context.shape)

# ===== Transformer Encoder tester =====
DiT = DiffusionTransformer(
    attention_embedding_size=attention_embedding_size,
    attention_heads=attention_heads,
    hidden_size=hidden_size,
    mlp_ratio=1,
    layernorm_affine=False,
    layernorm_epsilon=1e-6,
    perceptron_dropout_rate=0.0,
    perceptron_bias=True,
    perceptron_layernorm=True,
)

nn.init.constant_(DiT.adaptiveLN[-1].weight, 0)
nn.init.constant_(DiT.adaptiveLN[-1].bias, 0)
c = t + z

result = DiT(value, c)
print("Result final shape: ", result.shape)

# ===== Final layer testing =====
patch_size = 8
out_channels = 4

final = FinalLayer(hidden_size, patch_size, out_channels)
nn.init.constant_(final.AdaptiveLN[-1].weight, 0)
nn.init.constant_(final.AdaptiveLN[-1].bias, 0)
nn.init.constant_(final.linear.weight, 0)
nn.init.constant_(final.linear.bias, 0)

print("Final shape: ", final(result, c).shape)

# Mfw I need to do this because the author does not give out the expected tensor sizes
