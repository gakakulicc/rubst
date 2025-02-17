"""Runner for off-policy MA algorithms"""
import copy
import numpy as np
import torch
from harl.runners.off_policy_base_runner import OffPolicyBaseRunner


class OffPolicyMARunner(OffPolicyBaseRunner):
    """Runner for off-policy MA algorithms."""

    def train(self):
        """Train the model"""
        self.total_it += 1
        data = self.buffer.sample()
        (
            sp_share_obs,  # EP: (batch_size, dim), FP: (n_agents * batch_size, dim)
            sp_obs,  # (n_agents, batch_size, dim)
            sp_actions,  # (n_agents, batch_size, dim)
            sp_available_actions,  # (n_agents, batch_size, dim)
            sp_reward,  # EP: (batch_size, 1), FP: (n_agents * batch_size, 1)
            sp_done,  # EP: (batch_size, 1), FP: (n_agents * batch_size, 1)
            sp_valid_transition,  # (n_agents, batch_size, 1)
            sp_term,  # EP: (batch_size, 1), FP: (n_agents * batch_size, 1)
            sp_next_share_obs,  # EP: (batch_size, dim), FP: (n_agents * batch_size, dim)
            sp_next_obs,  # (n_agents, batch_size, dim)
            sp_next_available_actions,  # (n_agents, batch_size, dim)
            sp_gamma,  # EP: (batch_size, 1), FP: (n_agents * batch_size, 1)
        ) = data
        # train critic
        self.critic.turn_on_grad()
        if self.args["algo"] == "masac":
            next_actions = []
            next_logp_actions = []
            for agent_id in range(self.num_agents):
                next_action, next_logp_action = self.actor[
                    agent_id
                ].get_actions_with_logprobs(
                    sp_next_obs[agent_id],
                    sp_next_available_actions[agent_id]
                    if sp_next_available_actions is not None
                    else None,
                )
                next_actions.append(next_action)
                next_logp_actions.append(next_logp_action)
            self.critic.train(
                sp_share_obs,
                sp_actions,
                sp_reward,
                sp_done,
                sp_valid_transition,
                sp_term,
                sp_next_share_obs,
                next_actions,
                next_logp_actions,
                sp_gamma,
                self.value_normalizer,
            )
        else:
            next_actions = []
            for agent_id in range(self.num_agents):
                next_actions.append(
                    self.actor[agent_id].get_target_actions(sp_next_obs[agent_id])
                )
            self.critic.train(
                sp_share_obs,
                sp_actions,
                sp_reward,
                sp_done,
                sp_term,
                sp_next_share_obs,
                next_actions,
                sp_gamma,
            )
        self.critic.turn_off_grad()
        sp_valid_transition = torch.tensor(sp_valid_transition, device=self.device)
        if self.total_it % self.policy_freq == 0:
            # train actors
            # actions shape: (n_agents, batch_size, dim)
            if self.args["algo"] == "masac":
                logp_actions = []
                for agent_id in range(self.num_agents):
                    actions = copy.deepcopy(torch.tensor(sp_actions)).to(self.device)
                    self.actor[agent_id].turn_on_grad()
                    # train this agent
                    actions[agent_id], logp_action = self.actor[
                        agent_id
                    ].get_actions_with_logprobs(
                        sp_obs[agent_id],
                        sp_available_actions[agent_id]
                        if sp_available_actions is not None
                        else None,
                    )
                    logp_actions.append(logp_action)
                    actions_list = [a for a in actions]
                    if self.state_type == "EP":
                        actions_t = torch.cat(actions_list, dim=-1)
                    elif self.state_type == "FP":
                        logp_action = torch.tile(
                            logp_action, (self.num_agents, 1)
                        )
                        actions_t = torch.tile(
                            torch.cat(actions_list, dim=-1), (self.num_agents, 1)
                        )
                    value_pred = self.critic.get_values(sp_share_obs, actions_t)
                    if self.algo_args["algo"]["use_policy_active_masks"]:
                        if self.state_type == "EP":
                            actor_loss = (
                                    -torch.sum(
                                        (value_pred - self.alpha[agent_id] * logp_action)
                                        * sp_valid_transition[agent_id]
                                    )
                                    / sp_valid_transition[agent_id].sum()
                            )
                        elif self.state_type == "FP":
                            valid_transition = torch.tile(
                                sp_valid_transition[agent_id], (self.num_agents, 1)
                            )
                            actor_loss = (
                                    -torch.sum(
                                        (value_pred - self.alpha[agent_id] * logp_action)
                                        * valid_transition
                                    )
                                    / valid_transition.sum()
                            )
                    else:
                        actor_loss = -torch.mean(
                            value_pred - self.alpha[agent_id] * logp_action
                        )
                    self.actor[agent_id].actor_optimizer.zero_grad()
                    actor_loss.backward()
                    self.actor[agent_id].actor_optimizer.step()
                    self.actor[agent_id].turn_off_grad()
                    if self.algo_args["algo"]["auto_alpha"]:
                        log_prob = (
                                logp_action.detach()
                                + self.target_entropy[agent_id]
                        )
                        alpha_loss = -(self.log_alpha[agent_id] * log_prob).mean()
                        self.alpha_optimizer[agent_id].zero_grad()
                        alpha_loss.backward()
                        self.alpha_optimizer[agent_id].step()
                        self.alpha[agent_id] = torch.exp(
                            self.log_alpha[agent_id].detach()
                        )
                    actions[agent_id], _ = self.actor[
                        agent_id
                    ].get_actions_with_logprobs(
                        sp_obs[agent_id],
                        sp_available_actions[agent_id]
                        if sp_available_actions is not None
                        else None,
                    )
                # train critic's alpha
                if self.algo_args["algo"]["auto_alpha"]:
                    self.critic.update_alpha(logp_actions, np.sum(self.target_entropy))
            else:
                for agent_id in range(self.num_agents):
                    actions = copy.deepcopy(torch.tensor(sp_actions)).to(self.device)
                    self.actor[agent_id].turn_on_grad()
                    # train this agent
                    actions[agent_id] = self.actor[agent_id].get_actions(
                        sp_obs[agent_id], False
                    )
                    actions_list = [a for a in actions]
                    actions_t = torch.cat(actions_list, dim=-1)
                    value_pred = self.critic.get_values(sp_share_obs, actions_t)
                    actor_loss = -torch.mean(value_pred)
                    self.actor[agent_id].actor_optimizer.zero_grad()
                    actor_loss.backward()
                    self.actor[agent_id].actor_optimizer.step()
                    self.actor[agent_id].turn_off_grad()
                # soft update
                for agent_id in range(self.num_agents):
                    self.actor[agent_id].soft_update()
            self.critic.soft_update()
