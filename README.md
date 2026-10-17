# 阿里云 CDT 保活

香港抢占式实例 + 1GiB 系统盘 + CDT 每月 200G 免费公网流量 + 按流量自动启停，整机约 4.8 元/月。
思路来自 https://bbs.biliup.rs/t/topic/46 ，建机和保活全部脚本化，人只做下面几步。

## 你要做的事（都在阿里云控制台）

1. **RAM 子用户和 AK/SK**：RAM 控制台 → 身份管理 → 用户 → 创建用户。

    ![RAM 用户列表，点创建用户](docs/01-ram-create-user.png)

    填登录名，访问配置只勾"使用永久 AccessKey 访问"，再勾"我确认必须创建 AccessKey"，确定后立刻保存 AccessKey ID 和 Secret，Secret 只显示这一次。

    ![创建用户表单，勾选使用永久 AccessKey 访问和确认创建 AccessKey](docs/02-ram-user-accesskey.png)
2. **给这个子用户授权**（也就是给这对 AK 授权）：RAM → 用户 → 该用户 → 权限管理 → 新增授权，挂 6 个系统策略：AliyunECSFullAccess、AliyunOSSFullAccess、AliyunCDTFullAccess、AliyunVPCFullAccess、AliyunEIPFullAccess、AliyunCommonBandwidthPackageFullAccess（最后一个只在要建共享带宽时需要）。全是系统策略，不用自己写；`ram-policy-setup.json` 是想收窄权限时的最小化替代。挂完应该是这样：

    ![用户权限管理页，列出 OSS、ECS、CDT、VPC、EIP、共享带宽 6 个 FullAccess 系统策略](docs/03-ram-user-policies.png)
3. **开通 OSS 服务**：打开 oss.console.aliyun.com，没开通会看到下面这个页面，点"立即开通"（开通本身不收费）。

    ![OSS 控制台提示尚未开通，点立即开通](docs/04-oss-activate.png)
4. **授权镜像导入**：这一步和第 2 步无关。导入镜像是 ECS 这个服务去读你的 OSS bucket，用的不是你的 AK，而是扮演你账号里的一个角色，所以给 AK 再多权限也没用（实测：AK 已挂 AliyunOSSFullAccess 仍报 NoSetRoletoECSServiceAcount，把 AliyunECSImageImportRolePolicy 挂到用户上同样无效，它是给角色用的）。账号里要有角色 **AliyunECSImageImportDefaultRole**（名字是阿里云约定的默认值，想用别的名字就在 `import_image.py --role-name` 里指定），并且满足两点，缺一个都会报 `NoSetRoletoECSServiceAcount`：
    - 角色上挂系统策略 **AliyunECSImageImportRolePolicy**（RAM → 角色 → 该角色 → 权限管理 → 新增授权 → 系统策略，精确搜这个名字）
    - 信任策略允许 ECS 扮演，内容见 `ram-role-trust-ecs.json`（RAM → 角色 → 该角色 → 信任策略）

    最省事的做法是让控制台自动建：ECS 控制台 → 镜像 → 导入镜像 → 第一步"授予 ECS 对 OSS 资源的访问权限" → 同意授权，然后取消向导，两点都会配好。

    配好后逐项核对。RAM → 身份管理 → 角色，列表里要有这个角色，类型是普通角色：

    ![RAM 角色列表中的 AliyunECSImageImportDefaultRole](docs/05-ram-role-list.png)

    点进角色 → 权限管理，用"精确授权"搜 AliyunECSImageImportRolePolicy 挂上，这是最容易漏的一步：

    ![角色权限管理页，通过精确授权挂上 AliyunECSImageImportRolePolicy](docs/06-ram-role-policy.png)

    角色 → 信任策略，要允许 ecs.aliyuncs.com 扮演，和 `ram-role-trust-ecs.json` 一致：

    ![角色信任策略页，Principal Service 为 ecs.aliyuncs.com](docs/07-ram-role-trust.png)
5. **充值**：账户可用余额不低于 100 元，这是按量付费的门槛。


## 然后填配置

```bash
cp config.yaml.template config.yaml && chmod 600 config.yaml
```

用编辑器打开 config.yaml，把第 1 步的 AK/SK 填进 access_key_id 和 access_key_secret，保存。地域默认香港，其余参数都有默认值。`config.yaml` 已在 .gitignore 里，不要贴到聊天或提交。

剩下的交给 AI，它会读 `AGENTS.md` 按步骤执行。建完后它会问你两件事：保活脚本放哪台常开机器（可以让它帮你装进 crontab，也可以自己装，命令它会给），以及是否把第 2 步的权限收窄到 `ram-policy.json`（这一步要你在 RAM 控制台做，它会列出要解除的策略）。

## 文件

- `AGENTS.md`：给 AI 的执行手册（`CLAUDE.md` 引用它），剩下的事交给它
- `docs/errors.md`：报错对照；`docs/decisions.md`：为什么这样设计
- `config.yaml.template`：配置模板
- `ram-policy-setup.json`：第 2 步的最小化自定义策略（可选）；`ram-role-trust-ecs.json`：第 4 步角色的信任策略；`ram-policy.json`：保活阶段的最小权限
- `*.py`：脚本，用途见 AGENTS.md 仓库结构表
