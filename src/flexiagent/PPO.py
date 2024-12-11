from Agent import ValueS, MixedActor, Agent
import torch
from flexibuff import FlexiBatch
from torch.distributions import Categorical


class PPO(Agent):
    def __init__(
        self,
        obs_dim,
        continuous_action_dim=0,
        max_actions=None,
        min_actions=None,
        discrete_action_dims=None,
        lr_actor=0.0001,
        lr_critic=0.0003,
        gamma=0.99,
        eps_clip=0.2,
        n_epochs=10,
        device="cpu",
        entropy_loss=0.05,
    ):
        super().__init__()
        assert (
            continuous_action_dim > 0 or discrete_action_dims is not None
        ), "At least one action dim should be provided"
        self.device = device
        self.gamma = gamma
        self.eps_clip = eps_clip
        self.obs_dim = obs_dim
        self.continuous_action_dim = continuous_action_dim
        self.discrete_action_dims = discrete_action_dims
        self.n_epochs = n_epochs

        self.policy_loss = 1
        self.critic_loss = 1
        self.entropy_loss = entropy_loss

        self.actor = MixedActor(
            obs_dim=obs_dim,
            continuous_action_dim=continuous_action_dim,
            discrete_action_dims=discrete_action_dims,
            max_actions=max_actions,
            min_actions=min_actions,
            hidden_dims=[256, 256],
            device=device,
        )
        self.actor_old = MixedActor(
            obs_dim=obs_dim,
            continuous_action_dim=continuous_action_dim,
            discrete_action_dims=discrete_action_dims,
            max_actions=max_actions,
            min_actions=min_actions,
            hidden_dims=[256, 256],
            device=device,
        )
        self.actor_old.load_state_dict(self.actor.state_dict())
        self.critic = ValueS(state_size=obs_dim, hidden_size=256, device=self.device)

        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=lr_actor)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=lr_critic)

    def train_action(self, observations, legal_actions=None, step=False):
        if not torch.is_tensor(observations):
            observations = torch.tensor(observations, dtype=torch.float).to(self.device)
        if not torch.is_tensor(legal_actions) and legal_actions is not None:
            legal_actions = torch.tensor(legal_actions, dtype=torch.float).to(
                self.device
            )
        continuous_logits, descrete_logits = self.actor(
            x=observations, action_mask=legal_actions, gumbel=False, debug=False
        )
        continuous_dist = torch.distributions.Normal(
            loc=continuous_logits[: self.continuous_action_dim],
            scale=torch.exp(continuous_logits[self.continuous_action_dim :]),
        )
        descrete_dist = Multi(logits=descrete_logits)
        vals = self.critic(observations).detach()
        return act, log_probs[act].detach(), vals  # Action 0, log_prob 0

    # takes the observations and returns the action with the highest probability
    def deterministic_action(self, observations, legal_actions=None):
        act, probs, log_probs = self.actor.evaluate(
            observations, legal_actions=legal_actions
        )
        vals = self.critic(observations).detach()
        return act.argmax().item(), log_probs[act].detach(), vals

    def imitation_learn(self, observations, actions, legal_actions=None):
        if not torch.is_tensor(actions):
            actions = torch.tensor(actions, dtype=torch.int).to(self.device)
        if not torch.is_tensor(observations):
            observations = torch.tensor(observations, dtype=torch.float).to(self.device)

        act, probs, log_probs = self.actor.evaluate(
            observations, legal_actions=legal_actions
        )
        # max_actions = act.argmax(dim=-1, keepdim=True)
        # loss is MSE loss beteen the actions and the predicted actions
        oh_actions = torch.nn.functional.one_hot(
            actions.squeeze(-1), self.actor_size
        ).float()
        # print(oh_actions.shape, probs.shape)
        loss = torch.nn.functional.cross_entropy(probs, oh_actions, reduction="mean")
        self.actor_optimizer.zero_grad()
        loss.backward()
        self.actor_optimizer.step()

        return loss.item()  # loss

    def utility_function(self, observations, actions=None):
        if not torch.is_tensor(observations):
            observations = torch.tensor(observations, dtype=torch.float).to(self.device)
        if actions is not None:
            return self.critic(observations, actions)
        else:
            return self.critic(observations)
        # If actions are none then V(s)

    def expected_V(self, obs, legal_action):
        return self.critic(obs)

    def marl_learn(self, batch, agent_num, mixer, critic_only=False, debug=False):
        return super().marl_learn(batch, agent_num, mixer, critic_only, debug)

    def zero_grads(self):
        return 0

    def reinforcement_learn(
        self, batch: FlexiBatch, agent_num=0, critic_only=False, debug=False
    ):
        print(f"Doing PPO learn for agent {agent_num}")
        # Update the critic with Bellman Equation

        # Monte Carlo Estimate of returns
        G = torch.zeros_like(batch.global_rewards).to(self.device)
        G[-1] = batch.global_rewards[-1]
        for i in range(len(batch.global_rewards) - 2, 0, -1):
            G[i] = batch.global_rewards[i] + self.gamma * G[i + 1] * (
                1 - batch.terminated[i]
            )
        G = G.unsqueeze(-1)
        with torch.no_grad():
            advantages = G - self.critic(batch.obs)

        avg_actor_loss = 0
        # Update the actor
        legal_actions = None
        if batch.action_mask is not None:
            legal_actions = batch.action_mask[agent_num]
        for epoch in range(self.n_epochs):

            # with torch.no_grad():
            #     gar = 0
            #     if batch.global_auxiliary_rewards is not None:
            #         gar = batch.global_auxiliary_rewards.unsqueeze(-1)
            #     V_next = self.critic(batch.obs_[agent_num])
            #     # print(
            #     #    f"V_next: {V_next.shape}, batch.global_rewards: {batch.global_rewards.unsqueeze(-1).shape}, obs_: {batch.obs_[agent_num].shape}"
            #     # )
            #     V_targets = (
            #         batch.global_rewards.unsqueeze(-1)
            #         + gar
            #         + (self.gamma * (1 - batch.terminated.unsqueeze(-1)) * V_next)
            #     )
            V_current = self.critic(batch.obs[agent_num])
            loss = (V_current - G).square().mean()
            self.critic_optimizer.zero_grad()
            loss.backward()
            self.critic_optimizer.step()

            critic_loss = loss.item()
            print(f"critic_loss: {critic_loss}")

            act, probs, log_probs = self.actor.evaluate(
                batch.obs[agent_num], legal_actions=legal_actions
            )
            dist = Categorical(probs)
            dist_entropy = dist.entropy()
            selected_log_probs = torch.gather(
                input=log_probs,
                dim=-1,
                index=batch.discrete_actions[agent_num],  # act.unsqueeze(-1)
            )
            ratios = torch.exp(selected_log_probs - batch.discrete_log_probs[agent_num])
            # Calculate surrogate loss
            surr1 = ratios * advantages
            surr2 = (
                torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip) * advantages
            )
            actor_loss = (
                -self.policy_loss * torch.min(surr1, surr2).mean()
                + self.entropy_loss * dist_entropy.mean()
            )
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()
            avg_actor_loss += actor_loss.item()
            print(f"actor_loss: {actor_loss.item()}")

        avg_actor_loss /= self.n_epochs

        return avg_actor_loss, critic_loss

    def save(self, checkpoint_path):
        print("Save not implemeted")

    def load(self, checkpoint_path):
        print("Load not implemented")
