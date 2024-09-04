import os

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"]="0"

import torch
from einops import rearrange
import torch.nn as nn
from transformers import AutoTokenizer
from datasets import load_dataset
from safetensors.torch import load_model



def FeedForward(dim, expansion_factor=4):
	inner_dim = int(dim * expansion_factor)
	return nn.Sequential(
		nn.Linear(dim, inner_dim),
		nn.GELU(),
		nn.Linear(inner_dim, dim)
	)

def ConvForward(dim, expansion_factor=1):
	inner_dim = int(dim * expansion_factor)
	return nn.Sequential(
		nn.Conv1d(dim, inner_dim, 1),
		nn.GELU(),
		nn.Conv1d(inner_dim, dim, 1)
		)


class MixerBlock(nn.Module):

	def __init__(self, dim, length, mixer_mask=True, expand_conv=False):
		super().__init__()
		self.patch_layernorm = nn.LayerNorm(dim)
		self.seq_layernorm = nn.LayerNorm(dim)
		self.dim = dim
		self.length = length
		self.patch_ff = FeedForward(dim)
		if expand_conv:
			self.conv = ConvForward(length)
		else:
			self.conv = nn.Conv1d(length, length, 1)
		self.mixer_mask = mixer_mask
		self.expand_conv = expand_conv

	def forward(self, x: torch.tensor):
		if x.dim() > 3:
			x = rearrange(x, 'b p t f -> (b p) t f')

		# for CLM training, apply lower triangular mask to convolution weights
		if self.mixer_mask:
			if self.expand_conv:
				rearranged_shape = rearrange(self.conv[0].weight, 'f d p -> f (d p)').shape
				mask = torch.tril(torch.ones(rearranged_shape)).to(device)
				applied_mask = rearrange(self.conv[0].weight, 'f d p -> f (d p)') * mask
				self.conv[0].weight.data = rearrange(applied_mask, 'f (d p) -> f d p', p=1)

				rearranged_shape = rearrange(self.conv[2].weight, 'f d p -> f (d p)').shape
				mask = torch.tril(torch.ones(rearranged_shape)).to(device)
				applied_mask = rearrange(self.conv[2].weight, 'f d p -> f (d p)') * mask
				self.conv[2].weight.data = rearrange(applied_mask, 'f (d p) -> f d p', p=1)

			else:
				rearranged_shape = rearrange(self.conv.weight, 'f d p -> f (d p)').shape
				mask = torch.tril(torch.ones(rearranged_shape)).to(device)
				applied_mask = rearrange(self.conv.weight, 'f d p -> f (d p)') * mask
				self.conv.weight.data = rearrange(applied_mask, 'f (d p) -> f d p', p=1)

		residual = x
		x = self.seq_layernorm(x)
		x = self.conv(x) + residual
		residual = x
		x = self.patch_layernorm(x)
		x = self.patch_ff(x) + residual
		return x


class LanguageMixer(nn.Module):

	def __init__(self, n_vocab, dim, depth, tie_weights=False):
		super().__init__()
		self.wte = nn.Embedding(n_vocab, dim)
		self.mixerblocks = nn.ModuleList(
			[MixerBlock(
				dim = dim,
				length = tokenized_length,
				)
			for i in range(depth)]
			).to(device)
		self.lm_head = nn.Linear(dim, n_vocab, bias=False)
		if tie_weights:
			 self.wte.weight = self.lm_head.weight
		self.cel = nn.CrossEntropyLoss()

	def forward(self, input_ids, labels=None):
		x = input_ids
		x = x.to(device)
		x = self.wte(x)
		for block in self.mixerblocks:
			x = block(x)
		output = self.lm_head(x)
		labels = rearrange(labels, 'b p t -> b (p t)')
		output = rearrange(output, 'b t e -> b e t')
		labels = labels.to(device)
		shift_logits = output[..., :-1].contiguous()
		shift_labels = labels[..., 1:].contiguous()
		loss = self.cel(shift_logits[..., -100:], shift_labels[..., -100:])
		print (loss)
		return loss, output


class DoubleMixerBlock(nn.Module):

	def __init__(self, dim, length, clm_mask=True, expand_conv=False):
		super().__init__()
		self.patch_layernorm = nn.LayerNorm(dim)
		self.seq_layernormf = nn.LayerNorm(dim)
		self.seq_layernormr = nn.LayerNorm(dim)
		self.dim = dim
		self.length = length
		self.patch_ff = FeedForward(dim)
		if expand_conv:
			self.conv = ConvForward(length)
		else:
			self.convf = nn.Conv1d(length, length, 1)
			self.convr = nn.Conv1d(length, length, 1)
		self.clm_mask = clm_mask
		self.expand_conv = expand_conv
		self.softmax = nn.Softmax(dim=0)

	def forward(self, x: torch.tensor, y: torch.tensor):
		if x.dim() > 3:
			x = rearrange(x, 'b p t f -> (b p) t f')
			y = rearrange(y, 'b p t f -> (b p) t f')

		# for CLM training, apply lower triangular mask to convolution weights
		if self.clm_mask:
			if self.expand_conv:
				rearranged_shape = rearrange(self.conv[0].weight, 'f d p -> f (d p)').shape
				mask = torch.tril(torch.ones(rearranged_shape)).to(device)
				applied_mask = rearrange(self.conv[0].weight, 'f d p -> f (d p)') * mask
				self.conv[0].weight.data = rearrange(applied_mask, 'f (d p) -> f d p', p=1)

				rearranged_shape = rearrange(self.conv[2].weight, 'f d p -> f (d p)').shape
				mask = torch.tril(torch.ones(rearranged_shape)).to(device)
				applied_mask = rearrange(self.conv[2].weight, 'f d p -> f (d p)') * mask
				self.conv[2].weight.data = rearrange(applied_mask, 'f (d p) -> f d p', p=1)

			else:
				rearranged_shape = rearrange(self.conv.weight, 'f d p -> f (d p)').shape
				mask = torch.tril(torch.ones(rearranged_shape)).to(device)
				applied_mask = rearrange(self.conv.weight, 'f d p -> f (d p)') * mask
				self.conv.weight.data = rearrange(applied_mask, 'f (d p) -> f d p', p=1)

		else:

			masked_convf = torch.tril(rearrange(self.convf.weight, 'f d p -> p f d'))
			self.convf.weight.data = rearrange(masked_convf, 'p f d -> f d p').contiguous()

			masked_convr = torch.triu(rearrange(self.convr.weight, 'f d p -> p f d'), diagonal=2)
			self.convr.weight.data = rearrange(masked_convr, 'p f d -> f d p').contiguous()

		residualf, residualr = x, y
		x, y = self.seq_layernormf(x), self.seq_layernormr(y)
		x, y = self.convf(x) + residualf, self.convr(y) + residualr
		residualf, residualr = x, y
		x, y = self.patch_layernorm(x), self.patch_layernorm(y)
		x, y = self.patch_ff(x) + residualf, self.patch_ff(y) + residualr
		return x, y


class DoubleLanguageMixer(nn.Module):

	def __init__(self, n_vocab, dim, depth, tie_weights=False):
		super().__init__()
		self.wte = nn.Embedding(n_vocab, dim)
		self.mixerblocks = nn.ModuleList(
			[DoubleMixerBlock(
				dim = dim,
				length = tokenized_length,
				clm_mask=False
				)
			for i in range(depth)]
			).to(device)
		self.lm_head = nn.Linear(dim, n_vocab, bias=False)
		if tie_weights:
			 self.wte.weight = self.lm_head.weight
		self.cel = nn.CrossEntropyLoss()

	def forward(self, input_ids, labels=None, fonly=True):
		x = input_ids
		x = x.to(device)
		y = input_ids
		y = y.to(device)
		x, y = self.wte(x), self.wte(y)
		for block in self.mixerblocks:
			x, y = block(x, y)

		output = self.lm_head(x)
		if not fonly:
			output += self.lm_head(y)
		labels = rearrange(labels, 'b p t -> b (p t)')
		output = rearrange(output, 'b t e -> b e t')
		shift_logits = output[..., :-1].contiguous()
		shift_labels = labels[..., 1:].contiguous()
		loss = self.cel(shift_logits, shift_labels)
		return loss, output


def debatch_input(input_data):
	output = []
	for i in range(len(input_data)):
		if input_data[i].dim() > 1:
			input_data[i] = input_data[i].unsqueeze(1)
			output += list(input_data[i])
	return output


def batch_tokenize_input(train_text, test_text, length=2000, batch_size=1024):
	train_data, test_data = [], []
	max_length = 512

	for i in range(0, length, batch_size):
		input_ids = tokenizer.batch_encode_plus(
			train_text[i:i+batch_size]['text'],
			add_special_tokens=False,
			return_tensors='pt',
			truncation=True,
			max_length=max_length,
			padding='max_length'
		).input_ids
		train_data.append(input_ids)

	for i in range(0, len(test_text), batch_size):
		input_ids = tokenizer.batch_encode_plus(
			test_text[i:i+batch_size]['text'],
			add_special_tokens=False,
			return_tensors='pt',
			truncation=True,
			max_length=max_length,
			padding='max_length'
		).input_ids
		test_data.append(input_ids)

	train_data = debatch_input(train_data)
	test_data = debatch_input(test_data)

	return train_data, test_data

tokenizer = AutoTokenizer.from_pretrained("/home/bbadger/Desktop/tiny_token_4k")
tokenizer.pad_token = tokenizer.eos_token

train_text = load_dataset("roneneldan/TinyStories", split="train")
valid_text = load_dataset("roneneldan/TinyStories", split="validation")

train_data, test_data = batch_tokenize_input(train_text, valid_text)
# train_data, test_data = debatch_input(train_data), debatch_input(test_data)
n_vocab = len(tokenizer)

# barebones MLP mixer, expects an embedding on input tokens
tokenized_length = 512
dim = 1024
device = 'cuda' if torch.cuda.is_available() else 'cpu'
bmodel = DoubleLanguageMixer(n_vocab, dim, 8).float().to(device)
model = LanguageMixer(n_vocab, dim, 8).float().to(device)

# model.load_state_dict(torch.load('/home/bbadger/Desktop/tinystories_mixer_512_flat/checkpoint-424000/pytorch_model.bin'))

load_model(bmodel, '/home/bbadger/Desktop/tinystories_mixer_1024_n8_bmask/checkpoint-96000/model.safetensors')
load_model(model, '/home/bbadger/Desktop/tinystories/tinystories_mixer_1024_f_8/checkpoint-160000/model.safetensors')


# training_arguments = transformers.TrainingArguments(
# 	num_train_epochs=0,
# 	per_device_train_batch_size=16,
# 	per_device_eval_batch_size=1,
# 	warmup_steps=0,
# 	eval_steps=10,
# 	save_steps=200,
# 	learning_rate=2e-4,
# 	fp16=True, 
# 	evaluation_strategy='steps',
# 	output_dir='~/Desktop/tinystories_mixer_0',
# 	optim='adamw_torch',
# 	overwrite_output_dir=True,
# 	save_safetensors=False
# )

# trainer = transformers.Trainer(
# 	model=model,
# 	train_dataset=train_data,
# 	eval_dataset=test_data,
# 	args=training_arguments,
# 	data_collator=transformers.DataCollatorForLanguageModeling(tokenizer, mlm=False),
# )

# # prompt = 'Once upon a time, there was a big dog named Barky. He wagged his tail and began to'

# model.train()
# trainer.train('/home/bbadger/Desktop/tinystories_mixer/checkpoint-700000')
model.eval()
tokens = test_data[1]

prompt = '''

'''
# tokens = tokenizer.encode(prompt, return_tensors='pt', padding='max_length', max_length=512)

# tokens = tokenizer.encode(
# 				prompt,
# 				add_special_tokens=False,
# 				return_tensors='pt',
# 				padding='max_length',
# 				max_length=512
# 			)

# print ('model loaded.')
# print ('Input: ', tokenizer.decode(tokens[0]))
# tokens = rearrange(tokens, '(b p) t -> b p t', p=1)

# fout = []
# for i in range(50, 1, -1):
# 	loss, output = model(tokens, labels=tokens.to(device))
# 	out_token = torch.topk(output, dim=1, k=1).indices.flatten()[-i]
# 	tokens[..., -i+1] = out_token

# print ('\n \n')
# print ('Output: \n', tokenizer.decode(tokens[0][0]))


def double_inference(tokens, start_pos=50):

	print ('Full Input: ', tokenizer.decode(tokens[0]))
	print ('Truncated Input: ', tokenizer.decode(tokens[0][:-start_pos]))
	tokens = rearrange(tokens, '(b p) t -> b p t', p=1)

	for i in range(start_pos, 1, -1):
		loss, output = model(tokens, labels=tokens.to(device))
		out_token = torch.topk(output, dim=1, k=1).indices.flatten()[-i]
		tokens[..., -i+1] = out_token

	print ('\n \n')
	print ('Output: \n', tokenizer.decode(tokens[0][0]))

	for i in range(2, start_pos, 1):
		loss, output = bmodel(tokens, labels=tokens.to(device), fonly=False)
		out_token = torch.topk(output, dim=1, k=1).indices.flatten()[-i]
		tokens[..., -i+1] = out_token

	print ('\n \n')
	print ('Output: \n', tokenizer.decode(tokens[0][0]))


double_inference(tokens)