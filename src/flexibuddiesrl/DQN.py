import numpy as np
import torch.nn as nn
import torch
from torch.distributions import Categorical
from flexibuddiesrl.Agent import QS
from flexibuff import FlexiBatch
import os
import pickle

from enum import Enum


class dqntype(Enum):
    EGreedy = 0
    Soft = 1
    Munchausen = 2


class DQN(nn.Module):
    def __init__(
        self,
        obs_dim=10,
        discrete_action_dims=None,  # np.array([2]),
        continuous_action_dims=None,  # 2,
        min_actions=None,  # np.array([-1,-1]),
        max_actions=None,  # ,np.array([1,1]),
        hidden_dims=[64, 64],
        gamma=0.99,
        lr=3e-5,
        dueling=False,
        n_c_action_bins=10,
        munchausen=0,  # turns it into munchausen dqn
        entropy=0,  # turns it into soft-dqn
        activation="relu",
        orthogonal=False,
        init_eps=0.9,
        eps_decay_half_life=10000,
        device="cpu",
        eval_mode=False,
        name="DQN",
        clip_grad=1.0,
        load_from_checkpoint_path=None,
    ):
        super(DQN, self).__init__()
        self.clip_grad = clip_grad
        if load_from_checkpoint_path is not None:
            self.load(load_from_checkpoint_path)
            return
        self.eval_mode = eval_mode
        self.entropy_loss_coef = entropy  # use soft Q learning entropy loss or not H(Q)
        self.dqn_type = dqntype.EGreedy
        if self.entropy_loss_coef > 0:
            self.dqn_type = dqntype.Soft
        if self.entropy_loss_coef > 0 and munchausen > 0:
            self.dqn_type = dqntype.Munchausen

        self.obs_dim = obs_dim  # size of observation
        self.discrete_action_dims = discrete_action_dims
        # cardonality for each discrete action

        self.continuous_action_dims = continuous_action_dims
        # number of continuous actions

        self.name = name
        self.min_actions = min_actions  # min continuous action value
        self.max_actions = max_actions  # max continuous action value
        if max_actions is not None:
            self.np_action_ranges = self.max_actions - self.min_actions
            self.action_ranges = torch.from_numpy(self.np_action_ranges).to(device)
            self.np_action_means = (self.max_actions + self.min_actions) / 2
            self.action_means = torch.from_numpy(self.np_action_means).to(device)
        self.gamma = gamma
        self.lr = lr
        self.dueling = (
            dueling  # whether or not to learn True: V+Adv = Q or False: Adv = Q
        )
        self.n_c_action_bins = n_c_action_bins  # number of discrete action bins to discretize continuous actions
        self.munchausen = munchausen  # use munchausen loss or not
        self.twin = False  # min(double q) to reduce bias
        self.init_eps = init_eps  # starting eps_greedy epsilon
        self.eps = self.init_eps
        self.eps_decay_half_life = (
            eps_decay_half_life  # eps cut in half every 'half_life' frames
        )
        self.step = 0
        self.hidden_dims = hidden_dims
        self.activation = activation
        self.orthogonal = orthogonal
        self.Q1 = QS(
            obs_dim=obs_dim,
            continuous_action_dim=continuous_action_dims,
            discrete_action_dims=discrete_action_dims,
            hidden_dims=hidden_dims,
            activation=activation,
            orthogonal=orthogonal,
            dueling=dueling,
            n_c_action_bins=n_c_action_bins,
            device=device,
        )

        self.Q1.to(device)

        self.device = device
        self.optimizer = torch.optim.Adam(self.Q1.parameters(), lr=lr)
        self.to(device)

        # These can be saved to remake the same DQN
        self.attrs = [
            "step",
            "entropy_loss_coef",
            "munchausen",
            "discrete_action_dims",
            "continuous_action_dims",
            "min_actions",
            "max_actions",
            "gamma",
            "lr",
            "dueling",
            "n_c_action_bins",
            "init_eps",
            "eps_decay_half_life",
            "device",
            "eval_mode",
            "hidden_dims",
            "activation",
        ]

    def _cont_from_q(self, cont_act):
        return (
            torch.argmax(torch.stack(cont_act, dim=0), dim=-1)
            / (self.n_c_action_bins - 1)
            - 0.5
        ) * self.action_ranges + self.action_means

    def _cont_from_soft_q(self, cont_act):
        pb = torch.softmax(torch.stack(cont_act, dim=0), dim=-1)
        return (
            Categorical(probs=pb).sample() / (self.n_c_action_bins - 1) - 0.5
        ) * self.action_ranges + self.action_means

    def _discretize_actions(self, continuous_actions):
        return torch.clamp(  # inverse of _cont_from_q
            torch.round(
                ((continuous_actions - self.action_means) / self.action_ranges + 0.5)
                * (self.n_c_action_bins - 1)
            ).to(torch.int64),
            0,
            self.n_c_action_bins - 1,
        )

    def _e_greedy_train_action(
        self, observations, action_mask=None, step=False, debug=False
    ):
        disc_act, cont_act = None, None
        if self.init_eps > 0.0:
            self.eps = self.init_eps * (
                1 - self.step / (self.step + self.eps_decay_half_life)
            )
        value = 0
        if self.init_eps > 0.0 and np.random.rand() < self.eps:
            if len(self.discrete_action_dims) > 0:
                disc_act = np.zeros(
                    shape=len(self.discrete_action_dims), dtype=np.int32
                )
                for i in range(len(self.discrete_action_dims)):
                    disc_act[i] = np.random.randint(0, self.discrete_action_dims[i])

            if self.continuous_action_dims > 0:
                cont_act = (
                    np.random.rand(self.continuous_action_dims) - 0.5
                ) * self.np_action_ranges + self.np_action_means

        else:
            with torch.no_grad():
                value, disc_act, cont_act = self.Q1(observations, action_mask)
                # select actions from q function
                # print(value, disc_act, cont_act)
                if len(self.discrete_action_dims) > 0:
                    d_act = np.zeros(len(disc_act), dtype=np.int32)
                    for i, da in enumerate(disc_act):
                        d_act[i] = torch.argmax(da).detach().cpu().item()
                    disc_act = d_act
                if self.continuous_action_dims > 0:
                    if debug:
                        print(
                            f"  cont act {cont_act}, argmax: {torch.argmax(torch.stack(cont_act,dim=0),dim=-1).detach().cpu()}"
                        )
                        print(
                            f"  Trying to store this in actions {((torch.argmax(torch.stack(cont_act,dim=0),dim=-1)/ (self.n_c_action_bins - 1) -0.5)* self.action_ranges+ self.action_means)} calculated from da: {cont_act} with ranges: {self.action_ranges} and means: {self.action_means}"
                        )
                    cont_act = self._cont_from_q(cont_act).cpu().numpy()
        return disc_act, cont_act

    def _soft_train_action(self, observations, action_mask, step, debug):
        disc_act, cont_act = None, None
        with torch.no_grad():
            value, disc_act, cont_act = self.Q1(observations, action_mask)
            if len(self.discrete_action_dims) > 0:
                dact = np.zeros(len(disc_act), dtype=np.int64)
                for i, da in enumerate(disc_act):
                    if self.step > 10000:
                        print(torch.softmax(da, dim=-1))
                    dact[i] = (
                        Categorical(probs=torch.softmax(da, dim=-1))
                        .sample()
                        .cpu()
                        .item()
                    )
                    if self.step > 10000:
                        print(dact)
                disc_act = dact  # had to store da temporarily to keep using disc_act
            if self.continuous_action_dims > 0:
                if debug:
                    print(
                        f"  cont act {cont_act}, argmax: {torch.argmax(torch.stack(cont_act,dim=0),dim=-1).detach().cpu()}"
                    )
                    print(
                        f"  Trying to store this in actions {((torch.argmax(torch.stack(cont_act,dim=0),dim=-1)/ (self.n_c_action_bins - 1) -0.5)* self.action_ranges+ self.action_means)} calculated from da: {cont_act} with ranges: {self.action_ranges} and means: {self.action_means}"
                    )
                cont_act = self._cont_from_soft_q(cont_act).cpu().numpy()
        return disc_act, cont_act

    def train_actions(self, observations, action_mask=None, step=False, debug=False):
        disc_act, cont_act = self._e_greedy_train_action(
            observations, action_mask, step, debug
        )
        # if self.dqn_type == dqntype.EGreedy or self.dqn_type == dqntype.Munchausen:

        # else:
        #    disc_act, cont_act = self._soft_train_action(
        #        observations, action_mask, step, debug
        #    )

        self.step += int(step)
        return disc_act, cont_act, 0, 0, 0

    def ego_actions(self, observations, action_mask=None):
        return 0

    def imitation_learn(self, observations, actions):
        return 0  # loss

    def utility_function(self, observations, actions=None):
        return 0  # Returns the single-agent critic for a single action.
        # If actions are none then V(s)

    def expected_V(self, obs, legal_action=None, debug=False):
        with torch.no_grad():
            value, dac, cac = self.Q1(obs, legal_action)
            if debug:
                print(f"value: {value}, dac: {dac}, cac: {cac}, eps: {self.eps}")
            if self.dueling:
                return value.cpu()  # TODO make sure this doesnt need to be item()

            dq = 0
            n = 0
            if len(self.discrete_action_dims) > 0:
                n += 1
                for hi, h in enumerate(dac):
                    a = torch.argmax(h, dim=-1)
                    bestq = h[a].item()
                    h[a] = 0
                    if legal_action is not None:
                        if torch.sum(legal_action[hi]) == 1:
                            otherq = (
                                bestq  # no other choices so 100% * only legal choice
                            )
                        else:
                            otherq = torch.sum(  # average of other choices
                                h * legal_action[hi], dim=-1
                            ) / (torch.sum(legal_action[hi], dim=-1) - 1)
                    else:
                        otherq = torch.sum(h, dim=-1) / (
                            self.discrete_action_dims[hi] - 1
                        )
                        if debug:
                            print(
                                f"{otherq} = self.eps * {torch.sum(h, dim=-1)} / ({self.discrete_action_dims[hi] - 1})"
                            )

                    qmean = (1 - self.eps) * bestq + self.eps * otherq
                    if debug:
                        print(
                            f"dq: {qmean} = {(1 - self.eps)} * {bestq} + {self.eps} * {otherq}"
                        )
                    dq += qmean
                dq = dq / len(self.discrete_action_dims)
            cq = 0
            if self.continuous_action_dims > 0:
                n += 1
                for h in cac:
                    a = torch.argmax(h, dim=-1)

                    bestq = h[a].item()
                    h[a] = 0
                    otherq = torch.sum(h, dim=-1) / (self.n_c_action_bins - 1)

                    if debug:
                        print(
                            f"cq: {(1 - self.eps) * bestq + self.eps * otherq} = {(1 - self.eps)} * {bestq} + {self.eps} * ({otherq})"
                        )
                    cq += (1 - self.eps) * bestq + self.eps * otherq
                cq = cq / self.continuous_action_dims

            return (value + (cq + dq) / (max(n, 1))).cpu().item()

    def reinforcement_learn(
        self, batch: FlexiBatch, agent_num=0, critic_only=False, debug=False
    ):
        if self.eval_mode:
            return 0, 0
        if debug:
            print("\nDoing Reinforcement learn \n")
        dqloss = 0
        cqloss = 0
        with torch.no_grad():
            dQ_ = 0
            cQ_ = 0
            next_values, next_disc_adv, next_cont_adv = self.Q1(batch.obs_[agent_num])
            dnv_ = 0
            cnv_ = 0
            if self.dueling:
                dnv_ = next_values
                cnv_ = (  # Reshaping this to match the shape of next_cont_adv
                    next_values.expand(-1, self.continuous_action_dims)
                    .unsqueeze(-1)
                    .expand(-1, -1, self.n_c_action_bins)
                )
            if debug:
                print(
                    f"next vals: {next_values}, next_disct_adv: {next_disc_adv}, next_cont_adv: {next_cont_adv}"
                )
                # print(f"stacked: {torch.stack(next_cont_adv, dim=1)}")
                # input()

            if (
                self.discrete_action_dims is not None
                and len(self.discrete_action_dims) > 0
            ):
                dQ_ = torch.zeros(
                    (batch.global_rewards.shape[0], len(self.discrete_action_dims)),
                    dtype=torch.float32,
                ).to(self.device)
                for i in range(len(self.discrete_action_dims)):
                    if self.dqn_type == dqntype.EGreedy:
                        dQ_[:, i] = torch.max(
                            next_disc_adv[i], dim=-1
                        ).values + dnv_.squeeze(-1)
                    elif self.dqn_type == dqntype.Soft:
                        dQ_[:, i] = torch.sum(
                            torch.softmax(next_disc_adv[i], dim=-1)
                            * (next_disc_adv[i] + dnv_),
                            dim=-1,
                        )

            if (
                self.continuous_action_dims is not None
                and self.continuous_action_dims > 0
            ):
                if self.dqn_type == dqntype.EGreedy:
                    cQ_ = torch.max(
                        (
                            torch.stack(next_cont_adv, dim=1)
                            + (cnv_ if self.dueling else 0)
                        ),
                        dim=-1,
                    ).values
                elif self.dqn_type == dqntype.Soft:
                    scq = torch.stack(next_cont_adv, dim=1)
                    next_probs = torch.softmax(scq, dim=-1)
                    cQ_ = torch.sum(next_probs * (scq + cnv_), dim=-1)

        values, disc_adv, cont_adv = self.Q1(batch.obs[agent_num])
        dnv = 0
        cnv = 0
        if self.dueling:
            dnv = values.squeeze(-1)
            cnv = values.expand(-1, self.continuous_action_dims).unsqueeze(-1)

        if self.discrete_action_dims is not None and len(self.discrete_action_dims) > 0:
            dQ = torch.zeros(
                (batch.global_rewards.shape[0], len(self.discrete_action_dims)),
                dtype=torch.float32,
            ).to(self.device)

            for i in range(len(self.discrete_action_dims)):
                dQ[:, i] = (
                    torch.gather(
                        disc_adv[i],
                        dim=-1,
                        index=batch.discrete_actions[agent_num, :, i].unsqueeze(-1),
                    ).squeeze(-1)
                    + dnv
                )

            if self.dqn_type == dqntype.EGreedy:
                dqloss = (
                    dQ
                    - batch.global_rewards.unsqueeze(-1)
                    - (self.gamma * (1 - batch.terminated)).unsqueeze(-1) * dQ_
                ) ** 2

            elif self.dqn_type == dqntype.Soft:
                dqloss = (
                    dQ
                    - batch.global_rewards.unsqueeze(-1)
                    - (self.gamma * (1 - batch.terminated)).unsqueeze(-1) * dQ_
                ) ** 2
                for h in range(len(disc_adv)):
                    probs = torch.softmax(disc_adv[h], dim=-1)
                    enloss = (
                        Categorical(probs=probs).entropy()
                        * 0.1
                        * self.entropy_loss_coef
                    )
                    # print(dqloss[:, h].shape, enloss.shape)
                    if torch.isnan(enloss).any():
                        print("NAN in entropy")
                        print(probs)
                        print(enloss)
                    if torch.isnan(dqloss).any():
                        print("NAN in dqloss")
                        print(dqloss)
                    dqloss[:, h] -= enloss
            else:
                dqloss = 0
                for h in range(len(disc_adv)):
                    with torch.no_grad():
                        next_probs = torch.softmax(next_disc_adv[h], dim=-1)
                        lnprobs = torch.log(
                            torch.softmax(disc_adv[h], dim=-1).gather(
                                index=batch.discrete_actions[agent_num, :, h].unsqueeze(
                                    -1
                                ),
                                dim=-1,
                            )
                        ).squeeze(-1)
                        dQ_ = torch.sum(
                            next_probs
                            * (
                                (next_disc_adv[h] + dnv_ if self.dueling else 0)
                                - self.entropy_loss_coef * torch.log(next_probs)
                            ),
                            dim=-1,
                        )
                    dqloss += (
                        dQ[:, h]
                        - batch.global_rewards
                        - (self.munchausen * self.entropy_loss_coef * lnprobs)
                        - (self.gamma * (1 - batch.terminated)) * dQ_
                    ) ** 2
            dqloss = dqloss.mean()

        if self.continuous_action_dims is not None and self.continuous_action_dims > 0:
            cQ = (
                torch.gather(
                    torch.stack(cont_adv, dim=1),
                    dim=-1,
                    index=self._discretize_actions(
                        batch.continuous_actions[agent_num]
                    ).unsqueeze(-1),
                )
                + (cnv if self.dueling else 0)
            ).squeeze(-1)

            if self.dqn_type == dqntype.EGreedy:
                cqloss = (
                    cQ
                    - (
                        batch.global_rewards.unsqueeze(-1)
                        + (self.gamma * (1 - batch.terminated)).unsqueeze(-1) * cQ_
                    )
                ) ** 2
            elif self.dqn_type == dqntype.Soft:
                cqloss = (
                    cQ
                    - (
                        batch.global_rewards.unsqueeze(-1)
                        + (self.gamma * (1 - batch.terminated)).unsqueeze(-1) * cQ_
                    )
                ) ** 2
                cprob = torch.softmax(torch.stack(cont_adv, dim=1), dim=-1)
                cqloss -= (
                    Categorical(probs=cprob).entropy() * self.entropy_loss_coef
                )  # torch.sum(cprob * torch.log(cprob), dim=-1)
            else:
                cqloss = 0
                with torch.no_grad():
                    stacknc = torch.stack(next_cont_adv, dim=1)
                    next_probs = torch.softmax(stacknc, dim=-1)
                    lnprobs = torch.log(
                        torch.softmax(torch.stack(cont_adv, dim=1), dim=-1)
                        .gather(
                            index=self._discretize_actions(
                                batch.continuous_actions[agent_num]
                            ).unsqueeze(-1),
                            dim=-1,
                        )
                        .squeeze(-1)
                    )
                    cQ_ = torch.sum(
                        next_probs
                        * (
                            (stacknc + cnv_ if self.dueling else 0)
                            - self.entropy_loss_coef * torch.log(next_probs)
                        ),
                        dim=-1,
                    )
                cqloss = (
                    cQ
                    - batch.global_rewards.unsqueeze(-1)
                    - (self.munchausen * self.entropy_loss_coef * lnprobs)
                    - (self.gamma * (1 - batch.terminated).unsqueeze(-1)) * cQ_
                ) ** 2
            cqloss = cqloss.mean()
        loss = dqloss + cqloss
        self.optimizer.zero_grad()
        loss.backward()
        if self.clip_grad is not None and self.clip_grad > 0:
            torch.nn.utils.clip_grad_norm_(
                self.parameters(),
                self.clip_grad,
                error_if_nonfinite=True,
                foreach=True,
            )
        self.optimizer.step()

        return (
            dqloss.item() if torch.is_tensor(dqloss) else dqloss,
            cqloss.item() if torch.is_tensor(cqloss) else cqloss,
        )  # actor loss, critic loss

    def _dump_attr(self, attr, path):
        f = open(path, "wb")
        pickle.dump(attr, f)
        f.close()

    def _load_attr(self, path):
        f = open(path, "rb")
        d = pickle.load(f)
        f.close()
        return d

    def save(self, checkpoint_path):
        if self.eval_mode:
            print("Not saving because model in eval mode")
            return
        if checkpoint_path is None:
            checkpoint_path = "./" + self.name + "/"
        if not os.path.exists(checkpoint_path):
            os.makedirs(checkpoint_path)
        torch.save(self.Q1.state_dict(), checkpoint_path + "/Q1")
        for i in range(len(self.attrs)):
            self._dump_attr(
                self.__dict__[self.attrs[i]], checkpoint_path + f"/{self.attrs[i]}"
            )

    def load(self, checkpoint_path):
        if checkpoint_path is None:
            checkpoint_path = "./" + self.name + "/"

        for i in range(len(self.attrs)):
            self.__dict__[self.attrs[i]] = self._load_attr(
                checkpoint_path + f"/{self.attrs[i]}"
            )

        self.dqn_type = dqntype.EGreedy
        if self.entropy_loss_coef > 0:
            self.dqn_type = dqntype.Soft
        if self.entropy_loss_coef > 0 and self.munchausen > 0:
            self.dqn_type = dqntype.Munchausen
        if self.max_actions is not None:
            self.np_action_ranges = self.max_actions - self.min_actions
            self.action_ranges = torch.from_numpy(self.np_action_ranges).to(self.device)
            self.np_action_means = (self.max_actions + self.min_actions) / 2
            self.action_means = torch.from_numpy(self.np_action_means).to(self.device)

        self.Q1 = QS(
            obs_dim=self.obs_dim,
            continuous_action_dim=self.continuous_action_dims,
            discrete_action_dims=self.discrete_action_dims,
            hidden_dims=self.hidden_dims,
            activation=self.activation,
            orthogonal=self.orthogonal,
            dueling=self.dueling,
            n_c_action_bins=self.n_c_action_bins,
            device=self.device,
        )
        self.Q1.load_state_dict(torch.load(checkpoint_path + "/Q1"))
        self.Q1.to(self.device)

        self.optimizer = torch.optim.Adam(self.Q1.parameters(), lr=self.lr)
        self.to(self.device)

    def __str__(self):
        st = ""

        for i in self.__dict__.keys():
            st += f"i: {self.__dict__[i]}"

        return st


if __name__ == "__main__":
    obs_dim = 3
    continuous_action_dim = 2

    obs = np.random.rand(obs_dim).astype(np.float32)
    obs_ = np.random.rand(obs_dim).astype(np.float32)
    obs_batch = np.random.rand(14, obs_dim).astype(np.float32)
    obs_batch_ = obs_batch + 0.1

    dacs = np.stack(
        (np.random.randint(0, 4, size=(14)), np.random.randint(0, 5, size=(14))),
        axis=-1,
    )

    mem = FlexiBatch(
        registered_vals={
            "obs": np.array([obs_batch]),
            "obs_": np.array([obs_batch_]),
            "continuous_actions": np.array([np.random.rand(14, 2).astype(np.float32)]),
            "discrete_actions": np.array([dacs], dtype=np.int64),
            "global_rewards": np.random.rand(14).astype(np.float32),
        },
        terminated=np.random.randint(0, 2, size=14),
    )
    mem.to_torch("cuda:0")

    # print(f"expected v: {agent.expected_V(obs, legal_action=None)}")
    # exit()

    agent = DQN(
        obs_dim=obs_dim,
        continuous_action_dims=continuous_action_dim,
        max_actions=np.array([1, 2]),
        min_actions=np.array([0, 0]),
        discrete_action_dims=[4, 5],
        hidden_dims=[32, 32],
        device="cuda:0",
        lr=0.001,
        activation="relu",
    )
    d_acts, c_acts, d_log, c_log, _ = agent.train_actions(obs, step=True, debug=True)
    print(f"Training actions: c: {c_acts}, d: {d_acts}, d_log: {d_log}, c_log: {c_log}")
    aloss, closs = agent.reinforcement_learn(mem, 0, critic_only=False, debug=True)
    print("Finished Testing")

    agent = DQN(
        obs_dim=obs_dim,
        continuous_action_dims=continuous_action_dim,
        max_actions=np.array([1, 2]),
        min_actions=np.array([0, 0]),
        discrete_action_dims=[4, 5],
        hidden_dims=[32, 32],
        device="cuda:0",
        lr=0.001,
        activation="relu",
        entropy=0.1,
    )
    d_acts, c_acts, d_log, c_log, _ = agent.train_actions(obs, step=True, debug=True)
    print(f"Training actions: c: {c_acts}, d: {d_acts}, d_log: {d_log}, c_log: {c_log}")
    aloss, closs = agent.reinforcement_learn(mem, 0, critic_only=False, debug=True)
    print("Finished Testing")

    agent = DQN(
        obs_dim=obs_dim,
        continuous_action_dims=continuous_action_dim,
        max_actions=np.array([1, 2]),
        min_actions=np.array([0, 0]),
        discrete_action_dims=[4, 5],
        hidden_dims=[32, 32],
        device="cuda:0",
        lr=0.001,
        activation="relu",
        entropy=0.1,
        munchausen=0.5,
    )
    d_acts, c_acts, d_log, c_log, _ = agent.train_actions(obs, step=True, debug=True)
    print(f"Training actions: c: {c_acts}, d: {d_acts}, d_log: {d_log}, c_log: {c_log}")
    aloss, closs = agent.reinforcement_learn(mem, 0, critic_only=False, debug=True)
    print("Finished Testing")
