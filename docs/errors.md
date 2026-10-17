# 报错对照

按阶段排列。"处理"栏标【用户】的必须由人在控制台完成，其余由脚本或 AI 处理。

| 报错 / 现象 | 阶段 | 含义 | 处理 |
| --- | --- | --- | --- |
| `InvalidProtocol.NeedSsl` | 任意 | STS/部分接口只收 HTTPS | 已在 `aliyun_common.call` 强制 https；若自写请求记得 `set_protocol_type("https")` |
| `InvalidAccessKeyId` / `SignatureDoesNotMatch` | check_config | AK/SK 填错或有空格 | 【用户】重新填 config.yaml |
| 这对 AK 实际属于 UID … 与配置的不一致 | check_config | config 里填的 uid 和 AK 不匹配 | 【用户】改 uid 或删掉 uid 行 |
| `NoPermission`（cdt:*） | check_config / plan | 没有 CDT 权限 | 【用户】挂 AliyunCDTFullAccess（README 第 2 步） |
| `Forbidden.RAM`（vpc/EIP/共享带宽接口） | plan / provision | 缺 VPC、EIP 或共享带宽权限 | 【用户】挂 AliyunVPCFullAccess、AliyunEIPFullAccess、AliyunCommonBandwidthPackageFullAccess |
| OSS 403 `UserDisable`（EC 0003-00000801） | import_image / oss_bridge | 账号未开通 OSS，或欠费 | 【用户】OSS 控制台立即开通（README 第 3 步）；授权解决不了 |
| OSS `Read timed out` | import_image | 本机到香港上行太慢（实测约 90KB/s 时 8MB 分片超时） | 改用 `oss_bridge.py --local-dir .` 中转，再跑 `import_image.py` |
| `NoSetRoletoECSServiceAcount` | import_image | ECS 服务没有可扮演的角色去读 OSS。给 AK 加 OSS 权限无效 | 【用户】角色 AliyunECSImageImportDefaultRole 存在 + 挂 AliyunECSImageImportRolePolicy + 信任 ecs.aliyuncs.com（README 第 4 步）。角色叫别的名字时用 `import_image.py --role-name` |
| `InvalidAccountStatus.NotEnoughBalance` | provision / oss_bridge | 余额不足 100 元，按量付费下不了单 | 【用户】充值（README 第 5 步），然后重跑同一命令 |
| `RISK.RISK_CONTROL_REJECTION`（CreateCommonBandwidthPackage） | plan / provision | 账号被风控拦截创建共享带宽，无官方解释 | 跳过，`--no-cbwp`，EIP 自带 200Mbps 够用；用户想要可提工单 |
| `IncorrectInstanceStatus.Initializing` | DeleteInstance | 实例刚建还在初始化，不能删 | 等几秒重试（oss_bridge 已内置重试） |
| `InvalidSystemDiskCategory.ValueNotSupported` | DescribePrice / RunInstances | 盘类型和大小不匹配：公共镜像最小 20GiB；cloud_efficiency 不支持小盘；ESSD Entry 最小 10GiB | 1GiB 只能用自定义镜像 + cloud_essd PL0 |
| `SpotInterruptionBehavior` 相关报错 | provision | 该规格不支持被抢占后停机保留 | 脚本自动改为 Terminate 并提示；此时被回收会释放实例，保活只能重建 |
| `InvalidSecurityGroupId` | RunInstances DryRun | 还没建安全组 | 先跑 provision（它会建） |
| check_boot 截图里没有 `DataSourceAliYunLocal` | 验证 | cloud-init 没识别阿里云数据源，密钥没注入 | 换镜像：用 `--image alpine-iso`（需用户 VNC 手装）或 `--image debian`（20GiB 盘） |
| `ssh: Permission denied` | 验证 | 用了 root 或密钥对不对 | 用户名是 `alpine`；密钥对来自 `provision.py --ssh-key`（默认 ~/.ssh/id_rsa.pub） |
| 实例 … 不存在，可能已被释放 | keepalive | 实例被释放（Terminate 模式）或被手动删 | 重跑 provision 第 5 步重建 |
| StartInstances 失败 | keepalive | 抢占式库存不足 | 脚本每分钟自动重试，无需处理 |
| 担心 EIP 公网 IP 保有费 | 费用 | 直绑 VPC 内 ECS 不收，绑弹性网卡/NAT 才收 | 无需处理，见 [EIP 计费 FAQ](https://help.aliyun.com/zh/eip/support/billing-faq) |
