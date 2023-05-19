# Copyright (c) 2018-2022, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from enum import Enum
import numpy as np

from isaacgym import gymapi
from isaacgym import gymtorch
import torch

from env.tasks.humanoid import Humanoid, dof_to_obs
from utils import gym_util
from utils.motion_lib import MotionLib
from isaacgym.torch_utils import *
from isaacgym.terrain_utils import *



from utils import torch_utils

class HumanoidAMP(Humanoid):
    class StateInit(Enum):
        Default = 0
        Start = 1
        Random = 2
        Hybrid = 3

    def __init__(self, cfg, sim_params, physics_engine, device_type, device_id, headless):
        state_init = cfg["env"]["stateInit"]
        self._state_init = HumanoidAMP.StateInit[state_init]
        self._hybrid_init_prob = cfg["env"]["hybridInitProb"]
        self._num_amp_obs_steps = cfg["env"]["numAMPObsSteps"]  #这里就是amp_的obs了！！看看他关注了几步
        assert(self._num_amp_obs_steps >= 2)
        # self.num_env_sensor = cfg["env"]["num_env_sensor"]
        self._reset_default_env_ids = []
        self._reset_ref_env_ids = []


        super().__init__(cfg=cfg,
                         sim_params=sim_params,
                         physics_engine=physics_engine,
                         device_type=device_type,
                         device_id=device_id,
                         headless=headless)

        motion_file = cfg['env']['motion_file']
        self._load_motion(motion_file)
        self._amp_obs_buf = torch.zeros((self.num_envs, self._num_amp_obs_steps, self._num_amp_obs_per_step), device=self.device, dtype=torch.float)
        self._curr_amp_obs_buf = self._amp_obs_buf[:, 0]
        self._hist_amp_obs_buf = self._amp_obs_buf[:, 1:]
        self._interaction_amp = torch.zeros([self.num_envs, 3], device=self.device, dtype=torch.float)
        
        self._amp_obs_demo_buf = None
        self.create_terrains()

        return

    def post_physics_step(self):
        super().post_physics_step()
        
        self._update_hist_amp_obs()
        self._compute_amp_observations()

        amp_obs_flat = self._amp_obs_buf.view(-1, self.get_num_amp_obs())
        self.extras["amp_obs"] = amp_obs_flat

        return
    def get_num_env_sensor(self):
        return self.num_env_sensor

    def get_num_amp_obs(self):
        return self._num_amp_obs_steps * self._num_amp_obs_per_step

    def fetch_amp_obs_demo(self, num_samples):

        if (self._amp_obs_demo_buf is None):
            self._build_amp_obs_demo_buf(num_samples)
        else:
            assert(self._amp_obs_demo_buf.shape[0] == num_samples)
        
        motion_ids = self._motion_lib.sample_motions(num_samples)
        
        # since negative times are added to these values in build_amp_obs_demo,
        # we shift them into the range [0 + truncate_time, end of clip]
        truncate_time = self.dt * (self._num_amp_obs_steps - 1)
        motion_times0 = self._motion_lib.sample_time(motion_ids, truncate_time=truncate_time)
        motion_times0 += truncate_time

        amp_obs_demo = self.build_amp_obs_demo(motion_ids, motion_times0)
        self._amp_obs_demo_buf[:] = amp_obs_demo.view(self._amp_obs_demo_buf.shape)
        amp_obs_demo_flat = self._amp_obs_demo_buf.view(-1, self.get_num_amp_obs())

        return amp_obs_demo_flat

    def build_amp_obs_demo(self, motion_ids, motion_times0):
        dt = self.dt

        motion_ids = torch.tile(motion_ids.unsqueeze(-1), [1, self._num_amp_obs_steps])
        motion_times = motion_times0.unsqueeze(-1)
        time_steps = -dt * torch.arange(0, self._num_amp_obs_steps, device=self.device)
        motion_times = motion_times + time_steps

        motion_ids = motion_ids.view(-1)
        motion_times = motion_times.view(-1)
        root_pos, root_rot, dof_pos, root_vel, root_ang_vel, dof_vel, key_pos \
               = self._motion_lib.get_motion_state(motion_ids, motion_times)
        self._interaction_demo = root_pos - self._demo_object_pos
        amp_obs_demo = build_amp_observations(root_pos, root_rot, root_vel, root_ang_vel,
                                              dof_pos, dof_vel, key_pos,
                                              self._local_root_obs, self._root_height_obs,
                                              self._dof_obs_size, self._dof_offsets, self._interaction_demo)
        return amp_obs_demo

    def _build_amp_obs_demo_buf(self, num_samples):
        self._amp_obs_demo_buf = torch.zeros((num_samples, self._num_amp_obs_steps, self._num_amp_obs_per_step), device=self.device, dtype=torch.float32)
        return
        
    def _setup_character_props(self, key_bodies):
        super()._setup_character_props(key_bodies)

        asset_file = self.cfg["env"]["asset"]["assetFileName"]
        num_key_bodies = len(key_bodies)

        if (asset_file == "mjcf/amp_humanoid.xml"):
            self._num_amp_obs_per_step = 13 + self._dof_obs_size + 28 + 3 * num_key_bodies + 3# [root_h, root_rot, root_vel, root_ang_vel, dof_pos, dof_vel, key_body_pos]
        elif (asset_file == "mjcf/amp_humanoid_sword_shield.xml"):
            self._num_amp_obs_per_step = 13 + self._dof_obs_size + 31 + 3 * num_key_bodies + 3# [root_h, root_rot, root_vel, root_ang_vel, dof_pos, dof_vel, key_body_pos]
        else:
            print("Unsupported character config file: {s}".format(asset_file))
            assert(False)

        return

    def _load_motion(self, motion_file):
        assert(self._dof_offsets[-1] == self.num_dof)
        self._motion_lib = MotionLib(motion_file=motion_file,
                                     dof_body_ids=self._dof_body_ids,
                                     dof_offsets=self._dof_offsets,
                                     key_body_ids=self._key_body_ids.cpu().numpy(), 
                                     device=self.device)
        return
    
    def _reset_envs(self, env_ids):
        self._reset_default_env_ids = []
        self._reset_ref_env_ids = []

        super()._reset_envs(env_ids)
        self._init_amp_obs(env_ids)

        return

    def _reset_actors(self, env_ids):
        if (self._state_init == HumanoidAMP.StateInit.Default):
            self._reset_default(env_ids)
        elif (self._state_init == HumanoidAMP.StateInit.Start
              or self._state_init == HumanoidAMP.StateInit.Random):
            self._reset_ref_state_init(env_ids)
        elif (self._state_init == HumanoidAMP.StateInit.Hybrid):
            self._reset_hybrid_state_init(env_ids)
        else:
            assert(False), "Unsupported state initialization strategy: {:s}".format(str(self._state_init))
        return
    
    def _reset_default(self, env_ids):
        self._humanoid_root_states[env_ids] = self._initial_humanoid_root_states[env_ids]
        self._dof_pos[env_ids] = self._initial_dof_pos[env_ids]
        self._dof_vel[env_ids] = self._initial_dof_vel[env_ids]
        [env_ids]
        self._reset_default_env_ids = env_ids
        return

    def _reset_ref_state_init(self, env_ids):
        num_envs = env_ids.shape[0]
        motion_ids = self._motion_lib.sample_motions(num_envs)
        
        if (self._state_init == HumanoidAMP.StateInit.Random
            or self._state_init == HumanoidAMP.StateInit.Hybrid):
            motion_times = self._motion_lib.sample_time(motion_ids)
        elif (self._state_init == HumanoidAMP.StateInit.Start):
            motion_times = torch.zeros(num_envs, device=self.device)
        else:
            assert(False), "Unsupported state initialization strategy: {:s}".format(str(self._state_init))

        root_pos, root_rot, dof_pos, root_vel, root_ang_vel, dof_vel, key_pos \
               = self._motion_lib.get_motion_state(motion_ids, motion_times)

        self._set_env_state(env_ids=env_ids, 
                            root_pos=root_pos, 
                            root_rot=root_rot, 
                            dof_pos=dof_pos, 
                            root_vel=root_vel, 
                            root_ang_vel=root_ang_vel, 
                            dof_vel=dof_vel)

        self._reset_ref_env_ids = env_ids
        self._reset_ref_motion_ids = motion_ids
        self._reset_ref_motion_times = motion_times
        return

    def _reset_hybrid_state_init(self, env_ids):
        num_envs = env_ids.shape[0]
        ref_probs = to_torch(np.array([self._hybrid_init_prob] * num_envs), device=self.device)
        ref_init_mask = torch.bernoulli(ref_probs) == 1.0

        ref_reset_ids = env_ids[ref_init_mask]
        if (len(ref_reset_ids) > 0):
            self._reset_ref_state_init(ref_reset_ids)

        default_reset_ids = env_ids[torch.logical_not(ref_init_mask)]
        if (len(default_reset_ids) > 0):
            self._reset_default(default_reset_ids)

        return

    def stage_stairs_terrain(self, terrain, step_width, step_height, platform_size=1.):
        """
        Generate stairs

        Parameters:
            terrain (terrain): the terrain
            step_width (float):  the width of the step [meters]
            step_height (float): the step_height [meters]
            platform_size (float): size of the flat platform at the center of the terrain [meters]
        Returns:
            terrain (SubTerrain): update terrain
        """
        # switch parameters to discrete units
        step_width = int(step_width / terrain.horizontal_scale)
        step_height = int(step_height / terrain.vertical_scale)
        platform_size = int(platform_size / terrain.horizontal_scale)

        height = 0
        start_x = int(1.5 / terrain.horizontal_scale)
        stop_x = terrain.width - start_x
        start_y = int(1.5 / terrain.horizontal_scale)
        stop_y = terrain.length - start_y
        stair_num = 3
        for i in np.arange(stair_num):
            start_x += step_width
            stop_x -= step_width
            start_y += step_width
            stop_y -= step_width
            height += step_height
            terrain.height_field_raw[start_x: stop_x, start_y: stop_y] = height
        return terrain.height_field_raw

    def create_terrains(self):
        self._num_terrains = self.num_envs
        num_per_row = np.sqrt(self.num_envs)

        terrain_width = 10.
        terrain_length = 10.

        horizontal_scale = 0.04  # [m]
        self._horizontal_scale = horizontal_scale
        vertical_scale = 0.02  # [m]
        self._vertical_scale = vertical_scale
        num_rows = int(terrain_width / horizontal_scale)  # 高度图为的数组为num_row*num_cols  保存在terrain里面， 乘horizontal_scale以后是真实的大小
        num_cols = int(terrain_length / horizontal_scale)  # vertical_scale是高度的比例，其中terrain生成函数里面的输入就是真实的大小，如果scale（像素的）的大小比真实大小还大就会发生用斜坡补充的情况
        def new_sub_terrain():  return SubTerrain(width=num_rows, length=num_cols, vertical_scale=vertical_scale,
                                                 horizontal_scale=horizontal_scale)

        self.obs_heightfield = self.stage_stairs_terrain(new_sub_terrain(), step_width=0.32, step_height=0.16)  # 这个用来作网络输入
        self.heightfield = np.zeros((int(num_per_row) * num_rows, (int(self._num_terrains/int(num_per_row))+2) * num_cols))  # 这个用来记录环境的大env
        self.obs_heightfield_cuda = torch.tensor(self.obs_heightfield, device=self.device)
        for i in range(int(num_per_row)):
            for j in range(int(self._num_terrains/int(num_per_row))+1):
                self.heightfield[i * num_rows: (i+1) * num_rows, j * num_cols: (j+1) * num_cols] =  self.obs_heightfield
        vertices, triangles = convert_heightfield_to_trimesh(self.heightfield, horizontal_scale=horizontal_scale,
                                                             vertical_scale=vertical_scale, slope_threshold=1.5)
        tm_params = gymapi.TriangleMeshParams()
        tm_params.nb_vertices = vertices.shape[0]
        tm_params.nb_triangles = triangles.shape[0]
        tm_params.transform.p.x = 0
        tm_params.transform.p.y = 0
        tm_params.transform.p.z = -0.01
        self.gym.add_triangle_mesh(self.sim, vertices.flatten(), triangles.flatten(), tm_params)
        return

    def _init_amp_obs(self, env_ids):
        self._compute_amp_observations(env_ids)

        if (len(self._reset_default_env_ids) > 0):
            self._init_amp_obs_default(self._reset_default_env_ids)

        if (len(self._reset_ref_env_ids) > 0):
            self._init_amp_obs_ref(self._reset_ref_env_ids, self._reset_ref_motion_ids,
                                   self._reset_ref_motion_times)
        
        return

    def _init_amp_obs_default(self, env_ids):
        curr_amp_obs = self._curr_amp_obs_buf[env_ids].unsqueeze(-2)
        self._hist_amp_obs_buf[env_ids] = curr_amp_obs
        return

    def _init_amp_obs_ref(self, env_ids, motion_ids, motion_times):
        dt = self.dt
        motion_ids = torch.tile(motion_ids.unsqueeze(-1), [1, self._num_amp_obs_steps - 1])
        motion_times = motion_times.unsqueeze(-1)
        time_steps = -dt * (torch.arange(0, self._num_amp_obs_steps - 1, device=self.device) + 1)
        motion_times = motion_times + time_steps

        motion_ids = motion_ids.view(-1)
        motion_times = motion_times.view(-1)
        root_pos, root_rot, dof_pos, root_vel, root_ang_vel, dof_vel, key_pos \
               = self._motion_lib.get_motion_state(motion_ids, motion_times)
        interaction = root_pos[:] - self._object_pos[0]
        amp_obs_demo = build_amp_observations(root_pos, root_rot, root_vel, root_ang_vel, 
                                              dof_pos, dof_vel, key_pos, 
                                              self._local_root_obs, self._root_height_obs, 
                                              self._dof_obs_size, self._dof_offsets, interaction)
        self._hist_amp_obs_buf[env_ids] = amp_obs_demo.view(self._hist_amp_obs_buf[env_ids].shape)
        return
    
    def _set_env_state(self, env_ids, root_pos, root_rot, dof_pos, root_vel, root_ang_vel, dof_vel):
        self._humanoid_root_states[env_ids, 0:3] = root_pos
        self._humanoid_root_states[env_ids, 3:7] = root_rot
        self._humanoid_root_states[env_ids, 7:10] = root_vel
        self._humanoid_root_states[env_ids, 10:13] = root_ang_vel
        
        self._dof_pos[env_ids] = dof_pos
        self._dof_vel[env_ids] = dof_vel
        return

    def _update_hist_amp_obs(self, env_ids=None):  #因为amp_obs是一个连续片段,所以这里需要移动他,不过看起来用的也是很普通的位移操作
        if (env_ids is None):
            for i in reversed(range(self._amp_obs_buf.shape[1] - 1)):
                self._amp_obs_buf[:, i + 1] = self._amp_obs_buf[:, i]
        else:
            for i in reversed(range(self._amp_obs_buf.shape[1] - 1)):   #reversed 倒序 11变成10，  对这个shape是10还是horizon length? 应该是10
                self._amp_obs_buf[env_ids, i + 1] = self._amp_obs_buf[env_ids, i]  #这个应该是个3维数，第二个表示记录的n个steps 待确认
        return
    
    def _compute_amp_observations(self, env_ids=None):
        key_body_pos = self._rigid_body_pos[:, self._key_body_ids, :]
        self._interaction_amp[:] = self._rigid_body_pos[:, 0] - self._object_pos[:]
        if (env_ids is None):  #第一个是最新的
            self._curr_amp_obs_buf[:] = build_amp_observations(self._rigid_body_pos[:, 0, :],
                                                               self._rigid_body_rot[:, 0, :],
                                                               self._rigid_body_vel[:, 0, :],
                                                               self._rigid_body_ang_vel[:, 0, :],
                                                               self._dof_pos, self._dof_vel, key_body_pos,
                                                               self._local_root_obs, self._root_height_obs, 
                                                               self._dof_obs_size, self._dof_offsets, self._interaction_amp[:])
        else:
            self._curr_amp_obs_buf[env_ids] = build_amp_observations(self._rigid_body_pos[env_ids][:, 0, :],
                                                                   self._rigid_body_rot[env_ids][:, 0, :],
                                                                   self._rigid_body_vel[env_ids][:, 0, :],
                                                                   self._rigid_body_ang_vel[env_ids][:, 0, :],
                                                                   self._dof_pos[env_ids], self._dof_vel[env_ids], key_body_pos[env_ids],
                                                                   self._local_root_obs, self._root_height_obs, 
                                                                   self._dof_obs_size, self._dof_offsets, self._interaction_amp[env_ids])
        return


#####################################################################
###=========================jit functions=========================###
#####################################################################

@torch.jit.script
def build_amp_observations(root_pos, root_rot, root_vel, root_ang_vel, dof_pos, dof_vel, key_body_pos,
                           local_root_obs, root_height_obs, dof_obs_size, dof_offsets, interaction):
    # type: (Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, bool, bool, int, List[int], Tensor) -> Tensor
    root_h = root_pos[:, 2:3]  # 这个是高度,root_pos的第一个数据被作为人体的当前位置，不知道是不是头啊 第三是高度看样子是从三维简化到二维了。
    # 输入数据包括 高度，，关键关节4*3
    heading_rot = torch_utils.calc_heading_quat_inv(root_rot)
    # 3维到2维
    if (local_root_obs):
        root_rot_obs = quat_mul(heading_rot, root_rot)  #四元数相乘
    else:
        root_rot_obs = root_rot
    root_rot_obs = torch_utils.quat_to_tan_norm(root_rot_obs)
    
    if (not root_height_obs):
        root_h_obs = torch.zeros_like(root_h)
    else:
        root_h_obs = root_h
    
    local_root_vel = quat_rotate(heading_rot, root_vel)
    local_root_ang_vel = quat_rotate(heading_rot, root_ang_vel)

    root_pos_expand = root_pos.unsqueeze(-2)
    local_key_body_pos = key_body_pos - root_pos_expand
    
    heading_rot_expand = heading_rot.unsqueeze(-2)
    heading_rot_expand = heading_rot_expand.repeat((1, local_key_body_pos.shape[1], 1))
    flat_end_pos = local_key_body_pos.view(local_key_body_pos.shape[0] * local_key_body_pos.shape[1], local_key_body_pos.shape[2])
    flat_heading_rot = heading_rot_expand.view(heading_rot_expand.shape[0] * heading_rot_expand.shape[1], 
                                               heading_rot_expand.shape[2])
    local_end_pos = quat_rotate(flat_heading_rot, flat_end_pos)
    flat_local_key_pos = local_end_pos.view(local_key_body_pos.shape[0], local_key_body_pos.shape[1] * local_key_body_pos.shape[2])

    dof_obs = dof_to_obs(dof_pos, dof_obs_size, dof_offsets)  #应该是减少了一些数据量
    obs = torch.cat((root_h_obs, root_rot_obs, local_root_vel, local_root_ang_vel, dof_obs, dof_vel, flat_local_key_pos, interaction), dim=-1)
    return obs