import torch
import torch.nn as nn
import torch.nn.functional as F
import math
import collections
import numpy as np
import copy


from utils.spec_reader import spec

hidden_size = spec.val("DNC_HIDDEN_SIZE")
memory_size = spec.val("DNC_MEMORY_SIZE")
word_size = spec.val("DNC_WORD_SIZE")
num_write_heads = spec.val("DNC_NUM_WRITE_HEADS")
num_read_heads = spec.val("DNC_READ_HEADS")
num_read_modes = 1 + 2*num_write_heads
batch_size = 1

TemporalLinkageState = collections.namedtuple('TemporalLinkageState',
											  ('link', 'precedence_weights'))


AccessState = collections.namedtuple('AccessState', (
	'memory', 'read_weights', 'write_weights', 'linkage', 'usage'))

_EPSILON = 0.001

class MemoryModule(nn.Module):

	def __init__(self):
		super(MemoryModule, self).__init__()

		self.write_vectors_layer = nn.Linear(hidden_size, num_write_heads*word_size)
		self.erase_vectors_layer = nn.Linear(hidden_size, num_write_heads*word_size)
		self.free_gate_layer = nn.Linear(hidden_size, num_read_heads)
		self.allocation_gate_layer = nn.Linear(hidden_size, num_write_heads)
		self.write_gate_layer = nn.Linear(hidden_size, num_write_heads)
		self.read_mode_layer = nn.Linear(hidden_size, num_read_heads*num_read_modes)
		self.write_keys_layer = nn.Linear(hidden_size, num_write_heads*word_size)
		self.write_strengths_layer = nn.Linear(hidden_size, num_write_heads)
		self.read_keys_layer = nn.Linear(hidden_size, num_read_heads*word_size)
		self.read_strengths_layer = nn.Linear(hidden_size, num_read_heads)

	def forward(self, x, prev_state):

		read_output = self.read_inputs(x)

		#comment

		usage = self.computeUsage(read_output, prev_state)

		write_weights = self.computeWrite_weights(read_output, prev_state.memory, usage)
		#write_weights = torch.from_numpy(np.random.rand(batch_size, num_write_heads, memory_size)).float()

		memory = self.erase_and_write(prev_state.memory, write_weights,
		 	read_output['erase_vector'], read_output['write_vector'])

		#memory = torch.from_numpy(np.random.rand(batch_size, memory_size, word_size)).float()
		linkage_state = self.linkage(write_weights.detach(), prev_state.linkage)


		read_weights = self.computeReadWeights(read_output, memory.detach(), prev_state, linkage_state)

		read_words = torch.matmul(read_weights.detach(), memory.detach())

		#end comment

		######
		# initial_memory = torch.from_numpy(np.random.rand(batch_size, memory_size, word_size)).float()
		# initial_read_weights = torch.from_numpy(np.random.rand(batch_size, num_read_heads, memory_size)).float()
		# initial_write_weights = torch.from_numpy(np.random.rand(batch_size, num_write_heads, memory_size)).float()
		# #initial_linkage = np.random.rand(batch_size, num_write_heads, memory_size, memory_size)
		# initial_usage = torch.from_numpy(np.random.rand(batch_size, memory_size)).float()
		

		# initial_linkage_link = torch.from_numpy(np.random.rand(batch_size, num_write_heads, memory_size, memory_size)).float()
		# initial_linkage_precendence_weights = torch.from_numpy(np.random.rand(batch_size, num_write_heads, memory_size)).float()

		# initialLinkage = TemporalLinkageState(initial_linkage_link, initial_linkage_precendence_weights)

		# initial_access_state = AccessState(initial_memory, initial_read_weights, 
		# initial_write_weights, initialLinkage, initial_usage)

		########

		return (read_words, AccessState(memory=memory.detach(), read_weights=read_weights.detach(),
			write_weights=write_weights.detach(), linkage=linkage_state, usage=usage.detach()))




	def read_inputs(self, x):
		write_vector = torch.reshape(self.write_vectors_layer(x), (num_write_heads, word_size))
		erase_vector = torch.reshape(torch.sigmoid(self.erase_vectors_layer(x)), (num_write_heads, word_size))

		free_gate = torch.sigmoid(self.free_gate_layer(x))

		allocation_gate = torch.sigmoid(self.allocation_gate_layer(x))

		write_gate = torch.sigmoid(self.write_gate_layer(x))

		read_mode = torch.reshape(torch.sigmoid(self.read_mode_layer(x)), (num_read_heads, num_read_modes))

		write_keys = torch.reshape(torch.sigmoid(self.write_keys_layer(x)), (num_write_heads, word_size))

		write_strengths = self.write_strengths_layer(x)

		read_keys = torch.reshape(self.read_keys_layer(x), (num_read_heads, word_size))

		read_strengths = self.read_strengths_layer(x)


		result = {
		'write_vector': write_vector,
		'erase_vector': erase_vector,
		'free_gate': free_gate,
		'allocation_gate': allocation_gate,
		'write_gate': write_gate,
		'read_mode': read_mode,
		'write_keys': write_keys,
		'write_strengths': write_strengths,
		'read_keys':read_keys,
		'read_strengths':read_strengths
		}

		return result


	def computeUsage(self, inputs, prev_state):

		new_write_weights = prev_state.write_weights
		free_gate = inputs['free_gate']
		read_weights = prev_state.read_weights
		prev_usage = prev_state.usage


		write_weights = 1-torch.prod(new_write_weights, 1)
		usage = prev_usage + (1-prev_usage)*write_weights

		free_gate = torch.unsqueeze(free_gate, -1)

		free_read_weights = free_gate * read_weights

		phi = 1-torch.prod(free_read_weights, 1)

		return usage*phi


	def computeWrite_weights(self, read_inputs, memory, usage):
		memory_detached = copy.deepcopy(memory)

		dot = torch.matmul(read_inputs['write_keys'], torch.transpose(memory_detached, 1, 2))

		memory_norms = torch.sqrt(torch.sum(memory_detached*memory_detached, 2, keepdim=True))

		keys = read_inputs['write_keys']
		keys = torch.unsqueeze(keys, 0)

		intermed_keys = torch.sum(keys*keys, 2, keepdim=True)

		key_norms = torch.sqrt(intermed_keys)

		norm = torch.matmul(key_norms, torch.transpose(memory_norms, 1, 2))
		similarity = dot / (norm)
		write_content_weights = torch.zeros(batch_size,num_write_heads, word_size)


		write_gates=(read_inputs['allocation_gate'] * read_inputs['write_gate'])
		write_gates = torch.unsqueeze(write_gates, -1)
		allocation_weights = []


		for i in range(num_write_heads):
			###TODO check that sometimes beta is negative, that is not right, beta > 1
			#WARNING also this does not generalize to larger batches
			beta = read_inputs['write_strengths'][0][i]
			similarity_row = similarity[0][i][:]
			similarity_row = similarity_row * beta
			write_content_weights[0][i] = torch.nn.functional.softmax(similarity_row)

			allocation_weights.append(self._allocation(usage))
			usage += ((1 - usage) * write_gates[:, i, :] * allocation_weights[i])

		write_allocation_weights = torch.stack(allocation_weights, axis=1)


		allocation_gate = torch.unsqueeze(read_inputs['allocation_gate'].detach(), -1)
		write_gate = torch.unsqueeze(read_inputs['write_gate'].detach(), -1)

		return write_gate * (allocation_gate * write_allocation_weights +
		 				   (1 - allocation_gate) * write_content_weights)

	def _allocation(self, usage):

		usage = _EPSILON + (1 - _EPSILON) * usage


		sorted_usage, indices = torch.topk(usage, memory_size, largest=False, sorted=True)

		#this is not quite right, check

		prod_sorted_usage = torch.cumprod(sorted_usage, axis=1)
		exclusive_prod_sorted_usage = torch.ones(1,memory_size)
		exclusive_prod_sorted_usage[0][1:] = prod_sorted_usage[0][:-1]

		##end

		sorted_allocation = (1-sorted_usage)*exclusive_prod_sorted_usage

		final_allocation = torch.zeros((1, memory_size))
		#undo the initial permutation
		for i in range(memory_size):
			a_index = indices[0][i]
			final_allocation[0][a_index] = sorted_allocation[0][i]

		return final_allocation


	def erase_and_write(self, memory, address, reset_weights, values):

		memory_detached = copy.deepcopy(memory)

		expand_address = torch.unsqueeze(address, 3)
		reset_weights_expanded = torch.unsqueeze(reset_weights, 2)

		weighted_resets = expand_address*reset_weights_expanded

		#reset_gate = util.reduce_prod(1 - weighted_resets, 1)
		reset_gate = torch.prod(1-weighted_resets, 1)

		memory_detached *= reset_gate


		add_matrix = torch.matmul(torch.transpose(address, 1,2), values)

		memory_detached += add_matrix

		return memory_detached

	def linkage(self, write_weights, prev_linkage):
		prev_precedence_weights_tensor = prev_linkage.precedence_weights
		prev_link = prev_linkage.link
		batch_size = prev_linkage.link.shape[0]
		write_weights_i = torch.unsqueeze(write_weights, 3)
		write_weights_j = torch.unsqueeze(write_weights, 2)
		prev_precedence_weights_j = torch.unsqueeze(prev_precedence_weights_tensor, 2)
		prev_link_scale = 1 - write_weights_i - write_weights_j
		new_link = write_weights_i * prev_precedence_weights_j
		link = prev_link_scale * prev_link + new_link

		#this may not be right
		torch.diagonal(link).fill_(0)
		###

		write_sum = torch.sum(write_weights, 2)
		write_sum = torch.unsqueeze(write_sum, 2)

		precedence_weights = (1 - write_sum) * prev_precedence_weights_tensor + write_weights

		return TemporalLinkageState(link=link, precedence_weights=precedence_weights)

	def computeReadWeights(self, inputs, memory, prev_state, linkage_state):

		#memory = torch.from_numpy(memory).float()

		dot = torch.matmul(inputs['read_keys'], torch.transpose(memory, 1, 2))

		memory_norms = torch.sqrt(torch.sum(memory*memory, axis=2, keepdim=True))

		keys = inputs['read_keys']
		keys = torch.unsqueeze(keys, 0)

		key_norms = torch.sqrt(torch.sum(keys*keys, axis=2, keepdim=True))

		norm = torch.matmul(key_norms, torch.transpose(memory_norms, 1, 2))
		similarity = dot / (norm)

		##not entirely right, check

		content_weights = torch.nn.functional.softmax(similarity)

		####
		link = linkage_state.link

		read_weights = prev_state.read_weights.unsqueeze(0)
		print("read weights is")
		print(read_weights)

		# read_weights = torch.from_numpy(prev_state.read_weights).float()
		# print("read weights are")
		# print(read_weights)
		# print("link is")
		# print(link)

		#expanded_read_weights = torch.stack(read_weights * num_write_heads, 1)
		expanded_read_weights = read_weights * num_write_heads
		result = torch.matmul(expanded_read_weights, torch.transpose(link, 2, 3))
		result2 = torch.matmul(expanded_read_weights, link)

		forward_weights = result.permute(0, 2, 1, 3)
		backward_weights = result2.permute(0, 2, 1, 3)

		# forward_weights = torch.transpose(result, perm=[0, 2, 1, 3])

		# backward_weights = torch.transpose(result, perm=[0, 2, 1, 3])
		print("inputs read mode is")
		print(inputs['read_mode'].detach())

		batched_read_mode = torch.unsqueeze(inputs['read_mode'].detach(), 0)

		backward_mode = batched_read_mode[:,:, :num_write_heads]
		forward_mode = (batched_read_mode[:,:, num_write_heads:2 * num_write_heads])
		content_mode = batched_read_mode[:,:, 2 * num_write_heads]

		print("forward_mode size")
		print(forward_mode.size())
		print("Forward weights")
		print(forward_weights.size())

		# read_weights = (
		#   torch.unsqueeze(content_mode, 2) * content_weights + torch.sum(
		# 	  torch.unsqueeze(forward_mode, 3) * forward_weights, 2) +
		#   torch.unsqueeze(torch.unsqueeze(backward_mode, 3) * backward_weights, 2))

		read_weights = (
		  torch.unsqueeze(content_mode, 2) * content_weights)

		print("did this work")

		return read_weights

	def state_size(self):
		"""Returns a tuple of the shape of the state tensors."""
		return AccessState(
			memory=[memory_size, word_size],
			read_weights=[num_reads, memory_size],
			write_weights=[num_writes, memory_size],
			linkage=TemporalLinkageState(
				link=[num_writes, memory_size, memory_size],
				precedence_weights=[num_writes, memory_size],),
			usage=[memory_size])

		

