import networks
import tr_helpers
import experience
import tensorflow as tf
import numpy as np
import collections
import time
import ray
from collections import deque, OrderedDict
from tensorboardX import SummaryWriter
from tensorflow_utils import TensorFlowVariables


default_config = {
    'GAMMA' : 0.99,
    'LEARNING_RATE' : 1e-4,
    'EPSILON_DECAY_FRAMES' : 1e5,
    'NAME' : 'A2C',
    'SCORE_TO_WIN' : 20,
    'NETWORK' : networks.CartPoleA2C(),
    'ENV' : lambda : None, #gym.make('CartPole-v1')
    'REWARD_SHAPER' : tr_helpers.DefaultRewardsShaper(),
    'EPISODES_TO_LOG' : 20, 
    'LIVES_REWARD' : 5,
    'STEPS_NUM' : 1,
    'ENTROPY_COEF' : 0.001,
    'NUM_ACTORS' : 8
}

class Agent:
    def __init__(self, sess, actions_num, observation_shape, network, determenistic = False):
        self.sess = sess
        self.determenistic = determenistic
        self.obs_ph = tf.placeholder('float32', (None, ) + observation_shape)   
        self.actions , _ = network('agent', self.obs_ph, actions_num, reuse=True)
        self.softmax_probs = tf.nn.softmax(self.actions)
        self.variables = TensorFlowVariables(self.softmax_probs, self.sess)
        self.sess.run(tf.global_variables_initializer())

    def get_action_distribution(self, state):
        return self.sess.run(self.softmax_probs, {self.obs_ph: state})

    def set_weights(self, weights):
        self.variables.set_weights(weights)

    def get_action(self, state):
        policy = self.get_action_distribution([state])[0]
        if self.determenistic:
            action = np.argmax(policy)
        else:
            action = np.random.choice(len(policy), p=policy)
        return action

    

class NStepBuffer:
    def __init__(self, steps_num, env, agent, rewards_shaper):
        self.steps_num = steps_num
        self.env = env
        self.agent = agent
        self.rewards_shaper = rewards_shaper
        self.states = deque([], maxlen=self.steps_num)
        self._reset()
    
    def _reset(self):
        self.states.clear()
        self.current_state = self.env.reset()
        self.total_reward = 0.0
        self.total_shaped_reward = 0.0
        self.step_count = 0

    def play_steps(self):
        done_reward = None
        done_shaped_reward = None
        done_steps = None
        steps_rewards = 0
        cur_gamma = 1
        cur_states_len = len(self.states)
        # always break after one
        while True:
            if cur_states_len > 0:
                state = self.states[-1][0]
            else:
                state = self.current_state
            action = self.agent.get_action(state)
            new_state, reward, is_done, _ = self.env.step(action)
            reward = reward * (1 - is_done)
 
            self.step_count += 1
            self.total_reward += reward
            shaped_reward = self.rewards_shaper(reward)
            self.total_shaped_reward += shaped_reward
            self.states.append([new_state, action, shaped_reward])

            if len(self.states) < self.steps_num:
                break

            for i in range(self.steps_num):
                sreward = self.states[i][2]
                steps_rewards += sreward * cur_gamma
                cur_gamma = cur_gamma * 0.99

            next_state, current_action, _ = self.states[0]
            self.state = next_state
            break

        if is_done:
            done_reward = self.total_reward
            done_steps = self.step_count
            done_shaped_reward = self.total_shaped_reward
            self._reset()
        return [[state, current_action, shaped_reward, new_state, is_done], [done_reward, done_shaped_reward, done_steps]]

    

class A2CAgent:
    def __init__(self, sess, env_name, observation_shape, actions_num, config = default_config):
        self.network = config['NETWORK']
        self.env_creator = config['ENV']
        self.num_actors = config['NUM_ACTORS']
        self.config = config
        self.state_shape = observation_shape
        self.actions_num = actions_num
        self.writer = SummaryWriter()
        self.sess = sess
        self.gamma = self.config['GAMMA']
        self.obs_ph = tf.placeholder('float32', (None, ) + observation_shape)    
        self.next_obs_ph = tf.placeholder('float32', (None, ) + observation_shape)
        self.actions_ph = tf.placeholder('int32', (None,))
        self.rewards_ph = tf.placeholder('float32', (None,))
        self.is_done_ph = tf.placeholder('float32', (None,))
        self.actions, self.state_values = self.network('agent', self.obs_ph, actions_num, reuse=False)
        self.next_actions, self.next_state_values = self.network('agent', self.next_obs_ph, actions_num, reuse=True)
        self.probs = tf.nn.softmax(self.actions)
        self.log_probs = tf.nn.log_softmax(self.actions)
        self.next_state_values = self.next_state_values * (1 - self.is_done_ph)
        self.logp_actions = tf.reduce_sum(self.log_probs * tf.one_hot(self.actions_ph, actions_num), axis=-1)
        self.entropy = -tf.reduce_sum(self.probs * self.log_probs, 1, name="entropy")
        self.gamma_step = tf.constant(self.gamma**self.config['STEPS_NUM'], dtype=tf.float32)
        self.target_state_values = self.rewards_ph + self.gamma_step * self.next_state_values
        self.advantage = self.target_state_values - self.state_values
        self.actor_loss = -tf.reduce_mean(self.logp_actions * tf.stop_gradient(self.advantage)) - self.config['ENTROPY_COEF'] * tf.reduce_mean(self.entropy)
        self.critic_loss = tf.reduce_mean((self.state_values - tf.stop_gradient(self.target_state_values))**2 ) # TODO use huber loss too
        self.loss = self.actor_loss + self.critic_loss
        self.train_step = tf.train.AdamOptimizer(self.config['LEARNING_RATE']).minimize(self.loss)
        


        self.agent = Agent(sess, actions_num, observation_shape,  self.network)
        self.steps_buffer = NStepBuffer(self.config['STEPS_NUM'], envs[0], self.agent, self.config['REWARD_SHAPER'])
        self.sess.run(tf.global_variables_initializer())
        #actor_list = [remote_network.remote(x_ids[i], y_ids[i]) for i in range(NUM_BATCHES)]


    def get_action_distribution(self, state):
        return self.sess.run(self.probs, {self.obs_ph: state})


    def get_action(self, state, is_determenistic = False):
        policy = self.get_action_distribution([state])
        if is_determenistic:
            action = np.argmax(policy)
        else:
            action = np.random.choice(len(policy), p=policy)
        return action      


    def train(self):
        ind = 0
        while True:
            ind += 1
            #weights = TensorFlowVariables(self.actions, self.state_values)
            #self.agent.set_weights(weights)
            sars, done_info = self.steps_buffer.play_steps()

            if not (done_info[2] is None):
                print (done_info[2])
                if done_info[2] > 400:
                    print('done')
                    return
            dict = {self.obs_ph: [sars[0]], self.actions_ph : [sars[1]], self.rewards_ph : [sars[2]], self.next_obs_ph : [sars[3]], self.is_done_ph : [sars[4]] }
            a_loss, c_loss, entropy, _ = self.sess.run([self.actor_loss, self.critic_loss, self.entropy, self.train_step], dict)

            if ind % 100 == 0:
                print("a_loss", a_loss)
                print("c_loss", c_loss)
                print("entropy", entropy)

            
        