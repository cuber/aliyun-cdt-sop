# AGENTS.md — 阿里云 CDT 保活（AI 执行手册）

目标：在阿里云香港维持一台抢占式实例（2 核 0.5G，1GiB 系统盘，约 4.8 元/月），公网走 CDT 每月 200G 免费流量；流量到 180G 自动停机，次月 1 号自动开机。
人类只做控制台里的授权、开通和充值（README.md 的 5 步）；其余全部由你用本仓库脚本完成，用户不敲命令。
设计取舍见 `docs/decisions.md`，报错对照见 `docs/errors.md`。

## 硬性规则

1. 永远不要读取或打印 `config.yaml`（cat、grep、Read 都不行），它含真实 AK/SK。看结构用 `config.yaml.template`；验证凭证只跑 `check_config.py`，它的输出已脱敏。
2. 账号 UID、实例 ID、EIP、镜像 ID、bucket 名、角色 ID 不写进任何会提交或分享的文件、commit message 或聊天回复。它们只存在于 `state/`（已 gitignore）。
3. 所有资源按量付费。创建前先 `provision.py --plan` 看价格；删除或释放任何资源前必须让用户确认。
4. 脚本全部幂等，失败直接重跑。没有 `state/` 时按资源名复用：实例、VPC、安全组、EIP 都叫 `cdt-hk`，镜像叫 `alpine-cloud`。
5. 需要用户去控制台操作时，一次把缺的项列全并指向 README.md 的步骤号，不要分多轮问。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `README.md` | 给人看：控制台手工步骤（带截图）+ 填配置 |
| `AGENTS.md` | 本文件，AI 执行手册；`CLAUDE.md` 只是 `@AGENTS.md` |
| `docs/errors.md` | 报错 → 含义 → 处理 |
| `docs/decisions.md` | 为什么这样设计、费用、架构、清理与重建 |
| `docs/*.png` | README 用的截图，AI 不需要读 |
| `config.yaml.template` / `config.yaml` | 配置模板 / 真实配置（只需 AK/SK，不提交） |
| `aliyun_common.py` | 读配置、调 OpenAPI 的公共代码，从不打印密钥 |
| `status.py` | 一条命令看全局状态并给出下一步；`--json` 机器可读。接手时先跑它 |
| `check_config.py` | 校验凭证、找实例、测 CDT 权限 |
| `probe.py` | 探测地域现状、可用抢占规格与价格（可选） |
| `import_image.py` | 上传镜像到 OSS 并导入；`--status` 查进度；按镜像名复用 |
| `oss_bridge.py` | 本机上行慢时用临时实例中转镜像到 OSS，预签名 URL，AK 不落地 |
| `provision.py` | 建 VPC/交换机/安全组/密钥对/实例/EIP；`--plan` 只看不建 |
| `check_boot.py` | 拉实例串口输出和截图 |
| `keepalive.py` | 按 CDT 流量启停实例；`--dry-run` / `--loop` |
| `ram-policy-setup.json` | 建机阶段最小化自定义策略（可选，系统策略够用） |
| `ram-policy.json` | 保活阶段最小权限 |
| `ram-role-trust-ecs.json` | 镜像导入角色的信任策略 |
| `state/<name>.json` | 已建资源 ID，不含密钥，不提交 |

## 执行流程

所有命令在仓库根目录执行，Python 用 `.venv/bin/python`。

接手先跑一条：`.venv/bin/python status.py`（有 venv 和 config 之后）。它会逐账号给出凭证、缺的权限、镜像状态、实例状态、CDT 用量，最后一行"下一步"直接对应下表的步骤号；`--json` 得到同样内容的机器可读版。下表是每步的细节和失败处理。

| # | 检查 | 命令 | 成功标志 | 不满足时 |
| --- | --- | --- | --- | --- |
| 0 | 运行环境 | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` | 退出码 0 | 修依赖 |
| 1 | 配置存在 | 让用户执行 `cp config.yaml.template config.yaml && chmod 600 config.yaml` 并填 AK/SK | `config.yaml` 存在 | 等用户，不要代填 |
| 2 | 凭证有效 | `.venv/bin/python check_config.py` | 输出 `全部通过`；"还没有实例"是提示不是错误 | 按失败行查 `docs/errors.md` |
| 3 | 权限齐全 | `.venv/bin/python provision.py --plan` | 没有 `MISS` 行；`CreateCommonBandwidthPackage` 的 `RISK_CONTROL_REJECTION` 可忽略 | 把 MISS 对应的策略名列给用户（README 第 2 步），加好后重跑 |
| 4 | 镜像就绪 | `.venv/bin/python import_image.py --status` | `alpine-cloud … Available` | 执行下面"导入镜像" |
| 5 | 建机 | `.venv/bin/python provision.py --image alpine-cloud --no-cbwp` | 最后一行 `完成: 公网 IP …` | 报错查 `docs/errors.md`，修后重跑同一命令 |
| 6 | 实例可用 | 见"验证" | 四项都通过 | 查 `docs/errors.md` |
| 7 | 保活逻辑 | `.venv/bin/python keepalive.py --dry-run` | 输出 `期望 Running` 且实际 Running | 查 CDT 权限 |
| 8 | 保活部署 | 见"保活部署" | crontab 已加且日志正常 | 建完后主动问用户放哪台机器、由你装还是用户自己装 |

### 导入镜像（第 4 步不满足时）

```bash
V=generic_alpine-3.22.5-x86_64-bios-cloudinit-r0.qcow2
curl -LO https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/$V
curl -LO https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/$V.sha512
shasum -a 512 -c $V.sha512
.venv/bin/python import_image.py --file ./$V --name alpine-cloud --format QCOW2 --size 1
.venv/bin/python import_image.py --status        # 每 30 秒查一次，直到 Available，通常 2 分钟
```

- 镜像版本可换成 v3.22 目录下最新的 `generic_alpine-*-x86_64-bios-cloudinit-r0.qcow2`，虚拟盘只有 200MB，1GiB 系统盘够用。
- 上传慢于 1MB/s（分片超时）时改走中转：`.venv/bin/python oss_bridge.py --local-dir .`，它开一台临时 Debian 实例在香港下载后走内网上传，完成后自动释放；然后再跑一次 `import_image.py` 提交导入。
- 报 `UserDisable` 是 OSS 未开通（README 第 3 步）；报 `NoSetRoletoECSServiceAcount` 是镜像导入角色没配全（README 第 4 步）。都是用户操作，一次说清。

### 验证（第 6 步）

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 开机 | `.venv/bin/python check_boot.py` | 串口含 `Mounting root: ok`；截图里 cloud-init 打印 `Datasource DataSourceAliYunLocal` 和 `Authorized keys from /home/alpine/.ssh/authorized_keys` |
| SSH | `ssh -o StrictHostKeyChecking=no alpine@<EIP> 'cat /etc/alpine-release; df -h /'` | 返回 3.22.x，根分区约 950M |
| 出网 | `ssh alpine@<EIP> 'doas apk update && doas apk add -q curl && curl -s https://api.ipify.org'` | apk 能更新；返回的 IP 等于 EIP |
| 配置 | `.venv/bin/python check_config.py` | 实例行 `status=Running` |

EIP 从第 5 步输出或 `state/<name>.json` 的 `eip_ip` 取，不要写进回复以外的任何文件。

### 保活部署（第 8 步）

保活必须跑在被保活实例以外的常开机器上。第 5 到 7 步完成后，主动问用户两件事，不要等用户想起来：

1. **保活放哪台常开机器，由你装还是用户自己装。** 两条路径都可以：
    - 你代装（那台机器能 SSH 到时）：`ssh` 过去 `git clone` 本仓库，用 `scp` 把 `config.yaml` 传过去（不要经过聊天），重建 venv（第 0 步），跑 `keepalive.py --dry-run` 确认能读 CDT 流量和实例状态，然后追加 crontab：
      `(crontab -l 2>/dev/null; echo "* * * * * cd <仓库目录> && .venv/bin/python keepalive.py >> keepalive.log 2>&1") | crontab -`
      装完 `tail keepalive.log` 看到一行 `期望 Running` 即完成。
    - 用户自己装：把上面的 clone、scp、venv、dry-run、crontab 五条命令整理成可直接粘贴的清单给用户，告诉他看到日志里 `期望 Running` 就说明跑起来了。
    - 这台 Mac 不算常开机器，除非用户明确说就用它。
2. **是否把第 2 步的权限收窄到 `ram-policy.json`。** 要用户在 RAM 控制台操作：先用 `ram-policy.json` 建一个自定义策略挂到子用户，再解除 AliyunECSFullAccess、AliyunOSSFullAccess、AliyunCDTFullAccess、AliyunVPCFullAccess、AliyunEIPFullAccess、AliyunCommonBandwidthPackageFullAccess。收窄后跑 `check_config.py` 确认实例和 CDT 两行仍然通过。提醒用户：以后重建实例或重新导入镜像时要临时把这些权限加回来。

逻辑：CDT 本月流量 < 阈值（默认 180G）且实例 Stopped → 启动；≥ 阈值且 Running → 节省停机。实例建时设了"被抢占后停机保留"，被回收只停机不释放，下一轮自动重试启动。

## 人类必须做的事（API 做不了）

| 事项 | README 步骤 | 触发信号 |
| --- | --- | --- |
| 建 RAM 子用户并生成 AK/SK | 1 | 没有 config.yaml |
| 给子用户挂 6 个系统策略 | 2 | `provision.py --plan` 有 MISS，或 `Forbidden.RAM` / `NoPermission` |
| 开通 OSS 服务 | 3 | OSS 返回 `UserDisable` |
| 镜像导入角色 + AliyunECSImageImportRolePolicy + 信任策略 | 4 | `NoSetRoletoECSServiceAcount` |
| 充值到 100 元以上 | 5 | `InvalidAccountStatus.NotEnoughBalance` |
| 共享带宽（可选） | 无 | `RISK_CONTROL_REJECTION`，本方案跳过 |

## 清理与重建

- 可删且不影响运行：OSS bucket 里的镜像文件、没用的自定义镜像及快照。删前问用户。
- 保留 `alpine-cloud` 镜像；实例被释放时重跑第 5 步即可重建，VPC/安全组/密钥对/EIP 自动复用。
- 彻底下线的顺序：实例 → EIP（先解绑再释放，闲置 EIP 每小时收保有费）→ 安全组 → 交换机 → VPC → 镜像和快照 → OSS bucket。
