'''
MIT License

Copyright (c) 2017 Jack Chen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''

import os
import tensorflow as tf
import dill
import numpy as np
import zipfile
import tempfile
import time
from itertools import islice
from collections import deque

import baselines.common.tf_util as U

from baselines import logger
from baselines.common.schedules import LinearSchedule
from baselines import deepq
# Replay buffer or memory
from baselines.deepq.replay_buffer import ReplayBuffer, PrioritizedReplayBuffer

from pysc2.lib import actions as sc2_actions
from pysc2.env import environment
from pysc2.lib import features, actions

import gflags as flags

_PLAYER_RELATIVE = features.SCREEN_FEATURES.player_relative.index
_PLAYER_FRIENDLY = 1
_PLAYER_NEUTRAL = 3  # beacon/minerals
_PLAYER_HOSTILE = 4
_NO_OP = sc2_actions.FUNCTIONS.no_op.id
_MOVE_SCREEN = sc2_actions.FUNCTIONS.Move_screen.id
_ATTACK_SCREEN = sc2_actions.FUNCTIONS.Attack_screen.id
_SELECT_ARMY = sc2_actions.FUNCTIONS.select_army.id
_NOT_QUEUED = [0]
_SELECT_ALL = [0]

FLAGS = flags.FLAGS

class ActWrapper(object):
  def __init__(self, act):
    self._act = act
    #self._act_params = act_params

  @staticmethod
  def load(path, act_params, num_cpu=16):
    with open(path, "rb") as f:
      model_data = dill.load(f)
    act = deepq.build_act(**act_params)
    sess = U.make_session(num_cpu=num_cpu)
    sess.__enter__()
    with tempfile.TemporaryDirectory() as td:
      arc_path = os.path.join(td, "packed.zip")
      with open(arc_path, "wb") as f:
        f.write(model_data)

      zipfile.ZipFile(arc_path, 'r', zipfile.ZIP_DEFLATED).extractall(td)
      U.load_state(os.path.join(td, "model"))

    return ActWrapper(act)

  def __call__(self, *args, **kwargs):
    return self._act(*args, **kwargs)

  def save(self, path):
    """Save model to a pickle located at `path`"""
    with tempfile.TemporaryDirectory() as td:
      U.save_state(os.path.join(td, "model"))
      arc_name = os.path.join(td, "packed.zip")
      with zipfile.ZipFile(arc_name, 'w') as zipf:
        for root, dirs, files in os.walk(td):
          for fname in files:
            file_path = os.path.join(root, fname)
            if file_path != arc_name:
              zipf.write(file_path, os.path.relpath(file_path, td))
      with open(arc_name, "rb") as f:
        model_data = f.read()
    with open(path, "wb") as f:
      dill.dump((model_data), f)

def load(path, act_params, num_cpu=16):
  """Load act function that was returned by learn function.

  Parameters
  ----------
  path: str
      path to the act function pickle
  num_cpu: int
      number of cpus to use for executing the policy

  Returns
  -------
  act: ActWrapper
      function that takes a batch of observations
      and returns actions.
  """
  return ActWrapper.load(path, num_cpu=num_cpu, act_params=act_params)

def learn(env,
          q_func,
          num_actions=16,
          lr=5e-4,
          max_timesteps=100000,
          buffer_size=50000,
          exploration_fraction=0.1,
          exploration_final_eps=0.02,
          train_freq=1,
          batch_size=32,
          print_freq=1,
          checkpoint_freq=10000,
          learning_starts=1000,
          gamma=1.0,
          target_network_update_freq=500,
          prioritized_replay=False,
          prioritized_replay_alpha=0.6,
          prioritized_replay_beta0=0.4,
          prioritized_replay_beta_iters=None,
          prioritized_replay_eps=1e-6,
          num_cpu=16,
          param_noise=False,
          param_noise_threshold=0.05,
          callback=None):
  """Train a deepq model.

  Parameters
  -------
  env: pysc2.env.SC2Env
      environment to train on
  q_func: (tf.Variable, int, str, bool) -> tf.Variable
      the model that takes the following inputs:
          observation_in: object
              the output of observation placeholder
          num_actions: int
              number of actions
          scope: str
          reuse: bool
              should be passed to outer variable scope
      and returns a tensor of shape (batch_size, num_actions) with values of every action.
  lr: float
      learning rate for adam optimizer
  max_timesteps: int
      number of env steps to optimizer for
  buffer_size: int
      size of the replay buffer
  exploration_fraction: float
      fraction of entire training period over which the exploration rate is annealed
  exploration_final_eps: float
      final value of random action probability
  train_freq: int
      update the model every `train_freq` steps.
      set to None to disable printing
  batch_size: int
      size of a batched sampled from replay buffer for training
  print_freq: int
      how often to print out training progress
      set to None to disable printing
  checkpoint_freq: int
      how often to save the model. This is so that the best version is restored
      at the end of the training. If you do not wish to restore the best version at
      the end of the training set this variable to None.
  learning_starts: int
      how many steps of the model to collect transitions for before learning starts
  gamma: float
      discount factor
  target_network_update_freq: int
      update the target network every `target_network_update_freq` steps.
  prioritized_replay: True
      if True prioritized replay buffer will be used.
  prioritized_replay_alpha: float
      alpha parameter for prioritized replay buffer
  prioritized_replay_beta0: float
      initial value of beta for prioritized replay buffer
  prioritized_replay_beta_iters: int
      number of iterations over which beta will be annealed from initial value
      to 1.0. If set to None equals to max_timesteps.
  prioritized_replay_eps: float
      epsilon to add to the TD errors when updating priorities.
  num_cpu: int
      number of cpus to use for training
  callback: (locals, globals) -> None
      function called at every steps with state of the algorithm.
      If callback returns true training stops.

  Returns
  -------
  act: ActWrapper
      Wrapper over act function. Adds ability to save it and load it.
      See header of baselines/deepq/categorical.py for details on the act function.
  """
  # Create all the functions necessary to train the model

  sess = U.make_session(num_cpu)
  sess.__enter__()

  def make_obs_ph(name):
    return U.BatchInput((num_actions, num_actions), name=name)

  act_x, train_x, update_target_x, debug_x = deepq.build_train(
    make_obs_ph=make_obs_ph,
    q_func=q_func,
    num_actions=num_actions,
    optimizer=tf.train.AdamOptimizer(learning_rate=lr),
    gamma=gamma,
    grad_norm_clipping=10, 
    scope='deep_x'
  )

  act_y, train_y, update_target_y, debug_y = deepq.build_train(
    make_obs_ph=make_obs_ph,
    q_func=q_func,
    num_actions=num_actions,
    optimizer=tf.train.AdamOptimizer(learning_rate=lr),
    gamma=gamma,
    grad_norm_clipping=10, 
    scope='deep_y'
  )

  act_params = {
    'make_obs_ph': make_obs_ph,
    'q_func': q_func,
    'num_actions': num_actions,
  }
 
  # Create the replay buffer
  if prioritized_replay:
    replay_buffer_x = PrioritizedReplayBuffer(buffer_size, alpha=prioritized_replay_alpha)
    replay_buffer_y = PrioritizedReplayBuffer(buffer_size, alpha=prioritized_replay_alpha)

    if prioritized_replay_beta_iters is None:
      prioritized_replay_beta_iters = max_timesteps
    beta_schedule_x = LinearSchedule(prioritized_replay_beta_iters,
                                   initial_p=prioritized_replay_beta0,
                                   final_p=1.0)

    beta_schedule_y = LinearSchedule(prioritized_replay_beta_iters,
                                   initial_p=prioritized_replay_beta0,
                                   final_p=1.0)
  else:
    replay_buffer_x = ReplayBuffer(buffer_size)
    replay_buffer_y = ReplayBuffer(buffer_size)

    beta_schedule_x = None
    beta_schedule_y = None
  # Create the schedule for exploration starting from 1.
  exploration = LinearSchedule(schedule_timesteps=int(exploration_fraction * max_timesteps),
                               initial_p=1.0,
                               final_p=exploration_final_eps)  

  U.initialize()
  update_target_x()
  update_target_y()

  # Episode metrics
  episode_rewards = deque(maxlen=100)
  episode_beacons = deque(maxlen=100)
  episode_beacons_time = deque(maxlen=100)
  # episode_beacons_time / episode_beacons
  average_beacon_time = deque(maxlen=100)

  episode_rewards.append(0.0)
  episode_beacons.append(0.0)
  episode_beacons_time.append(0.0)

  num_episodes = 0
  saved_mean_reward = None

  obs = env.reset()
  # Select marines
  obs = env.step(actions=[sc2_actions.FunctionCall(_SELECT_ARMY, [_SELECT_ALL])])

  player_relative = obs[0].observation["screen"][_PLAYER_RELATIVE]

  screen = (player_relative == _PLAYER_NEUTRAL).astype(int)

  player_y, player_x = (player_relative == _PLAYER_FRIENDLY).nonzero()
  player = [int(player_x.mean()), int(player_y.mean())]
  #print(np.array(screen)[None].shape)

  reset = True
  with tempfile.TemporaryDirectory() as td:
    model_saved = False
    model_file = os.path.join("model/", "mineral_shards")
    print(model_file)

    beacon_time_start = 0
    beacons_collected = 0

    # __________________________________ THE MAIN LOOP ______________________________________________________________________________________

    for t in range(max_timesteps):
      if callback is not None:
        if callback(locals(), globals()):
          break
      # Take action and update exploration to the newest value
      kwargs = {}
      if not param_noise:
        update_eps = exploration.value(t)
        update_param_noise_threshold = 0.
      else:
        update_eps = 0.
        if param_noise_threshold >= 0.:
          update_param_noise_threshold = param_noise_threshold
        else:
          # Compute the threshold such that the KL divergence between perturbed and non-perturbed
          # policy is comparable to eps-greedy exploration with eps = exploration.value(t).
          # See Appendix C.1 in Parameter Space Noise for Exploration, Plappert et al., 2017
          # for detailed explanation.
          update_param_noise_threshold = -np.log(1. - exploration.value(t) + exploration.value(t) / float(num_actions))
        kwargs['reset'] = reset
        kwargs['update_param_noise_threshold'] = update_param_noise_threshold
        kwargs['update_param_noise_scale'] = True
      #print(np.array(screen)[None].shape)

      # Create the network output (action)
      action_x = act_x(np.array(screen)[None], update_eps=update_eps, **kwargs)[0]
      action_y = act_y(np.array(screen)[None], update_eps=update_eps, **kwargs)[0]

      reset = False

      coord = [player[0], player[1]]
      rew = 0
      
      beacon_time = 0
      
      coord = [action_x, action_y]

      change_x = coord[0] - player[0]
      change_y = coord[1] - player[1]
      change_m = np.sqrt((change_x ** 2) + (change_y ** 2))
      #print(change_y, change_x, change_m)

      # path_memory = np.array(path_memory_) # at end of action, edit path_memory
      if _MOVE_SCREEN not in obs[0].observation["available_actions"]:
        obs = env.step(actions=[sc2_actions.FunctionCall(_SELECT_ARMY, [_SELECT_ALL])])   
      else:
          new_action = [sc2_actions.FunctionCall(_MOVE_SCREEN, [_NOT_QUEUED, coord])]
          obs = env.step(actions = new_action)

      player_relative = obs[0].observation["screen"][_PLAYER_RELATIVE]
      new_screen = (player_relative == _PLAYER_NEUTRAL).astype(int)

      # Player coordinates cannot be determined when something else overlaps them
      try:
      	player_y, player_x = (player_relative == _PLAYER_FRIENDLY).nonzero()
      	player = [int(player_x.mean()), int(player_y.mean())]
      except ValueError:
      	#print(player_y, player_x)
      	pass
      
      screen_l = 16
      
      if obs[0].reward != 0:
      	#obs[0].reward has increased
        #beacons_collected += 1
      	beacon_time_end = t
      	beacon_time = beacon_time_end - beacon_time_start
      	beacon_time_start = t
      rew = obs[0].reward * 100

      # change_m is difference of clicked points 
      # compare to radius of circle half the area of screen
      #if change_m > np.sqrt((screen_l**2/2)/np.pi):
      #	rew -= 1
      # compare to raidus of circle quarter of area of screen
      #if change_m < np.sqrt((screen_l**2/4)/np.pi):
      #	rew += 1

      done = obs[0].step_type == environment.StepType.LAST

      replay_buffer_x.add(screen, action_x, rew, new_screen, float(done))
      replay_buffer_y.add(screen, action_y, rew, new_screen, float(done))

      screen = new_screen

      episode_rewards[-1] += rew
      episode_beacons[-1] += obs[0].reward
      episode_beacons_time[-1] += beacon_time

      if done:
        # Reset environment, player coordinates, and metrics
        obs = env.reset()
        player_relative = obs[0].observation["screen"][_PLAYER_RELATIVE]
        screen = (player_relative == _PLAYER_NEUTRAL).astype(int)

        player_y, player_x = (player_relative == _PLAYER_FRIENDLY).nonzero()
        player = [int(player_x.mean()), int(player_y.mean())]

        env.step(actions=[sc2_actions.FunctionCall(_SELECT_ARMY, [_SELECT_ALL])])

        if episode_beacons_time[-1] != 0.0 and episode_beacons[-1] != 0.0:
          average_beacon_time.append(episode_beacons_time[-1] / episode_beacons[-1])
        else:
          average_beacon_time.append(np.nan)
        episode_rewards.append(0.0)
        episode_beacons.append(0.0)
        episode_beacons_time.append(0.0)

        #beacons_collected = 0
        num_episodes += 1

        reset = True

      if t > learning_starts and t % train_freq == 0:
        # Minimize the error in Bellman's equation on a batch sampled from replay buffer.
        if prioritized_replay:

          experience_x = replay_buffer_x.sample(batch_size, beta=beta_schedule_x.value(t))
          (obses_t_x, actions_x, rewards_x, obses_tp1_x, dones_x, weights_x, batch_idxes_x) = experience_x

          experience_y = replay_buffer_y.sample(batch_size, beta=beta_schedule_y.value(t))
          (obses_t_y, actions_y, rewards_y, obses_tp1_y, dones_y, weights_y, batch_idxes_y) = experience_y

        else:
          
          obses_t_x, actions_x, rewards_x, obses_tp1_x, dones_x = replay_buffer_x.sample(batch_size)
          weights_x, batch_idxes_x = np.ones_like(rewards_x), None

          obses_t_y, actions_y, rewards_y, obses_tp1_y, dones_y = replay_buffer_y.sample(batch_size)
          weights_y, batch_idxes_y = np.ones_like(rewards_y), None

        td_errors_x = train_x(obses_t_x, actions_x, rewards_x, obses_tp1_x, dones_x, weights_x)

        td_errors_y = train_x(obses_t_y, actions_y, rewards_y, obses_tp1_y, dones_y, weights_y)

        if prioritized_replay:
          new_priorities_x = np.abs(td_errors_x) + prioritized_replay_eps
          new_priorities_y = np.abs(td_errors_y) + prioritized_replay_eps
          replay_buffer_x.update_priorities(batch_idxes_x, new_priorities_x)
          replay_buffer_y.update_priorities(batch_idxes_y, new_priorities_y)

      if t > learning_starts and t % target_network_update_freq == 0:
        # Update target network periodically.
        update_target_x() 
        update_target_y()
        
      mean_100ep_reward = round(np.mean(episode_rewards), 1)
      mean_100ep_beacon = round(np.mean(episode_beacons), 1)
      mean_100ep_beacon_time = np.nanmean(average_beacon_time)
      if done and print_freq is not None and len(episode_rewards) % print_freq == 0:
        logger.record_tabular("steps", t)
        logger.record_tabular("episodes", num_episodes)
        logger.record_tabular("mean 100 episode reward", mean_100ep_reward)
        logger.record_tabular("mean 100 episode beacon", mean_100ep_beacon)
        logger.record_tabular("% time spent exploring", int(100 * exploration.value(t)))
        logger.record_tabular("mean time between beacon", mean_100ep_beacon_time)
        logger.dump_tabular()

      if (checkpoint_freq is not None and t > learning_starts and
              num_episodes > 100 and t % checkpoint_freq == 0):
        if saved_mean_reward is None or mean_100ep_reward > (saved_mean_reward * 1.2):
          if print_freq is not None:
            logger.log("Saving model due to mean reward increase: {} -> {}".format(
              saved_mean_reward, mean_100ep_reward))
          U.save_state(model_file)
          model_saved = True
          saved_mean_reward = mean_100ep_reward
    if model_saved:
      if print_freq is not None:
        logger.log("Restored model with mean reward: {}".format(saved_mean_reward))
      U.load_state(model_file)

  return ActWrapper(act)
