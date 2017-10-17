import numpy as np
import os
import tensorflow as tf

# Load env
# Create DQN model with baselines
# Create replay buffer with baselines' built-in function
# Get action from DQN and map to action that is in ['available_actions']
# Execute action, save (s, a, r, s_) to memory
# (?) Something about shifting a matrix when army moves
