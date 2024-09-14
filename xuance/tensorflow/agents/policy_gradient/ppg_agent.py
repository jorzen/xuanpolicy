import numpy as np
from tqdm import tqdm
from copy import deepcopy
from argparse import Namespace
from xuance.environment import DummyVecEnv
from xuance.tensorflow import tf, Module
from xuance.tensorflow.utils import NormalizeFunctions, ActivationFunctions, InitializeFunctions
from xuance.tensorflow.policies import REGISTRY_Policy
from xuance.tensorflow.agents import OnPolicyAgent
from xuance.tensorflow.utils import split_distributions


class PPG_Agent(OnPolicyAgent):
    """The implementation of PPG agent.

    Args:
        config: the Namespace variable that provides hyper-parameters and other settings.
        envs: the vectorized environments.
    """

    def __init__(self,
                 config: Namespace,
                 envs: DummyVecEnv):
        super(PPG_Agent, self).__init__(config, envs)
        self.continuous_control = False
        self.policy_nepoch = config.policy_nepoch
        self.value_nepoch = config.value_nepoch
        self.aux_nepoch = config.aux_nepoch

        self.auxiliary_info_shape = {"old_dist": None}
        self.memory = self._build_memory(self.auxiliary_info_shape)  # build memory
        self.policy = self._build_policy()  # build policy
        self.learner = self._build_learner(self.config, self.policy)  # build learner.

    def _build_policy(self) -> Module:
        normalize_fn = NormalizeFunctions[self.config.normalize] if hasattr(self.config, "normalize") else None
        initializer = InitializeFunctions[self.config.initialize] if hasattr(self.config, "initialize") else None
        activation = ActivationFunctions[self.config.activation]

        # build representation.
        representation = self._build_representation(self.config.representation, self.observation_space, self.config)

        # build policy.
        if self.config.policy == "Categorical_PPG":
            policy = REGISTRY_Policy["Categorical_PPG"](
                action_space=self.action_space, representation=representation,
                actor_hidden_size=self.config.actor_hidden_size, critic_hidden_size=self.config.critic_hidden_size,
                normalize=normalize_fn, initialize=initializer, activation=activation)
            self.continuous_control = False
        elif self.config.policy == "Gaussian_PPG":
            policy = REGISTRY_Policy["Gaussian_PPG"](
                action_space=self.action_space, representation=representation,
                actor_hidden_size=self.config.actor_hidden_size, critic_hidden_size=self.config.critic_hidden_size,
                normalize=normalize_fn, initialize=initializer, activation=activation,
                activation_action=ActivationFunctions[self.config.activation_action])
            self.continuous_control = True
        else:
            raise AttributeError(f"PPG currently does not support the policy named {self.config.policy}.")

        return policy

    def action(self, observations: np.ndarray,
               return_dists: bool = False, return_logpi: bool = False):
        """Returns actions and values.

        Parameters:
            observations (np.ndarray): The observation.
            return_dists (bool): Whether to return dists.
            return_logpi (bool): Whether to return log_pi.

        Returns:
            actions: The actions to be executed.
            values: The evaluated values.
            dists: The policy distributions.
            log_pi: Log of stochastic actions.
        """
        shape_obs = observations.shape
        if len(shape_obs) > 2:
            observations = observations.reshape(-1, shape_obs[-1])
            if self.continuous_control:
                _, policy_mean, values, _ = self.policy(observations)
                policy_mean = policy_mean.numpy().reshape(shape_obs[:-1] + self.action_space.shape)
                policy_std = tf.exp(self.policy.actor.logstd).numpy()
                self.policy.actor.dist.set_param(policy_mean, policy_std)
            else:
                _, policy_logits, values, _ = self.policy(observations)
                policy_logits = policy_logits.numpy().reshape(shape_obs[:-1] + (self.action_space.n, ))
                self.policy.actor.dist.set_param(logits=policy_logits)
            values = tf.reshape(values, shape_obs[:-1])
        else:
            _, _, values, _ = self.policy(observations)
        policy_dists = self.policy.actor.dist
        actions = policy_dists.stochastic_sample()
        log_pi = policy_dists.log_prob(actions) if return_logpi else None
        dists = split_distributions(policy_dists) if return_dists else None
        actions = actions.numpy()
        values = values.numpy()
        return {"actions": actions, "values": values, "dists": dists, "log_pi": log_pi}

    def get_aux_info(self, policy_output: dict = None):
        """Returns auxiliary information.

        Parameters:
            policy_output (dict): The output information of the policy.

        Returns:
            aux_info (dict): The auxiliary information.
        """
        aux_info = {"old_dist": policy_output["dists"]}
        return aux_info

    def train(self, train_steps):
        obs = self.envs.buf_obs
        for _ in tqdm(range(train_steps)):
            step_info = {}
            self.obs_rms.update(obs)
            obs = self._process_observation(obs)
            policy_out = self.action(obs, return_dists=True, return_logpi=False)
            acts, rets = policy_out['actions'], policy_out['values']
            next_obs, rewards, terminals, trunctions, infos = self.envs.step(acts)
            aux_info = self.get_aux_info(policy_out)
            self.memory.store(obs, acts, self._process_reward(rewards), rets, terminals, aux_info)
            if self.memory.full:
                vals = self.get_terminated_values(next_obs, rewards)
                for i in range(self.n_envs):
                    if terminals[i]:
                        self.memory.finish_path(0.0, i)
                    else:
                        self.memory.finish_path(vals[i], i)
                # policy update
                indexes = np.arange(self.buffer_size)
                for _ in range(self.policy_nepoch):
                    np.random.shuffle(indexes)
                    for start in range(0, self.buffer_size, self.batch_size):
                        end = start + self.batch_size
                        sample_idx = indexes[start:end]
                        samples = self.memory.sample(sample_idx)
                        step_info.update(self.learner.update_policy(**samples))
                # critic update
                for _ in range(self.value_nepoch):
                    np.random.shuffle(indexes)
                    for start in range(0, self.buffer_size, self.batch_size):
                        end = start + self.batch_size
                        sample_idx = indexes[start:end]
                        samples = self.memory.sample(sample_idx)
                        step_info.update(self.learner.update_critic(**samples))

                # update old_prob
                buffer_obs = self.memory.observations
                buffer_act = self.memory.actions
                new_policy_out = self.action(buffer_obs, return_dists=True)
                aux_info = self.get_aux_info(new_policy_out)
                self.memory.auxiliary_infos.update(aux_info)
                for _ in range(self.aux_nepoch):
                    np.random.shuffle(indexes)
                    for start in range(0, self.buffer_size, self.batch_size):
                        end = start + self.batch_size
                        sample_idx = indexes[start:end]
                        samples = self.memory.sample(sample_idx)
                        step_info.update(self.learner.update_auxiliary(**samples))
                self.log_infos(step_info, self.current_step)
                self.memory.clear()

            self.returns = self.gamma * self.returns + rewards
            obs = deepcopy(next_obs)
            for i in range(self.n_envs):
                if terminals[i] or trunctions[i]:
                    self.ret_rms.update(self.returns[i:i + 1])
                    self.returns[i] = 0.0
                    if self.atari and (~trunctions[i]):
                        pass
                    else:
                        if terminals[i]:
                            self.memory.finish_path(0, i)
                        else:
                            vals = self.get_terminated_values(next_obs, rewards)
                            self.memory.finish_path(vals[i], i)
                        obs[i] = infos[i]["reset_obs"]
                        self.envs.buf_obs[i] = obs[i]
                        self.current_episode[i] += 1
                        if self.use_wandb:
                            step_info["Episode-Steps/env-%d" % i] = infos[i]["episode_step"]
                            step_info["Train-Episode-Rewards/env-%d" % i] = infos[i]["episode_score"]
                        else:
                            step_info["Episode-Steps"] = {"env-%d" % i: infos[i]["episode_step"]}
                            step_info["Train-Episode-Rewards"] = {"env-%d" % i: infos[i]["episode_score"]}
                        self.log_infos(step_info, self.current_step)
            self.current_step += self.n_envs
