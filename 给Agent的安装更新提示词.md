# 给 Agent 的安装 / 更新流程（策略师工作包）

本文件给 Codex、Claude Code 等 Agent 读。用户只需把 README 里那一段提示词贴给你；你按下面流程做，每一步把实际运行的输出给用户看，不要声称没做过的事。

## 0. 先弄清三件事

1. **仓库最新版本**：读 `https://raw.githubusercontent.com/tushigan/strategy-work-harness/main/版本清单.json`（读不到网络就直说，停下）。记下 `version`、`git_tag`、`release_zip.sha256`。
2. **用户要装到哪里**：
   - 当前目录（或用户指定的目录）里已经有 `project/state.json` 和 `scripts/project_handoff.py` → 这是**已有工作区**，走「二、更新」。
   - 没有 → 这是**新项目**，走「一、安装」。目录位置由用户给；没给就问一句，不要自己猜一个地方建。
3. **已安装的版本**（只在更新时）：读工作区 `README.md` 第 3 行附近 `当前版本：vX.Y.Z`；再算 `package-inventory.json` 的 SHA-256，和版本清单 `installed_version_detection.inventory_sha256_map` 对照。两者一致就是已装版本；对不上说明本机改过工作包文件，更新前先把差异列给用户。

同一版本、`python3 scripts/check_package_integrity.py --workspace .` 通过、`package-inventory.json` 哈希与版本清单一致 → **不重复覆盖**，告诉用户“已是最新版 vX.Y.Z，文件完整”，结束。

## 取得新版包（安装和更新都用）

优先用发布的 ZIP（可校验）：

```bash
mkdir -p "$TMPDIR/策略师工作包-下载" && cd "$TMPDIR/策略师工作包-下载"
curl -L -o 包.zip "<版本清单 release_zip.url>"
shasum -a 256 包.zip      # 必须等于版本清单 release_zip.sha256，不等就停
unzip -q 包.zip           # 解出 统一包/
```

ZIP 下不到时用 git：`git clone --depth 1 --branch <git_tag> https://github.com/tushigan/strategy-work-harness.git`，包内容在仓库的 `策略师工作包/` 目录。两种方式取到的包都要跑一次：

```bash
cd <包目录>
python3 scripts/package_audit.py audit --root . --mode template --json   # ok 必须为 true（按 package-inventory.json 逐文件核对哈希）
python3 scripts/check_package_integrity.py --workspace .                 # 两边入口完整
```

## 一、安装（新项目）

1. 把整个包目录（含隐藏的 `.agents`、`.claude`、`.codex`）复制到用户指定位置并改成项目名：`ditto <包目录> "<项目目录>"`。`ls -la` 确认三个隐藏目录都在；`project/state.json` 的 `status` 必须是 `not_initialized`（空白包）。
2. 在项目目录里跑并把输出给用户：
   ```bash
   python3 scripts/check_package_integrity.py --workspace . --json
   python3 scripts/sync_hosts.py check --workspace .
   python3 scripts/validate_project.py --root . --json        # errors 必须为空
   python3 scripts/check_environment.py --workspace . --json  # 缺什么如实列出
   python3 scripts/project_handoff.py resume --workspace . --json
   ```
3. 环境缺项的处理口径：Chrome + Playwright（视觉增量检核、对外导出）——用 `HARNESS_NODE_MODULES` 指向本机已有的含 playwright 的 `node_modules`，没有就告诉用户不设时视觉检核一律全页；`pypdfium2`（PDF 逐页比对）可 `pip install pypdfium2`；`@oai/artifact-tool` 只影响合同排期导出 Excel。**不要自行安装付费或需要账号的东西。**
4. 登记最小项目身份：问用户项目名、客户名、品牌名、项目编号（没有就留空），按包内 `运行环境与命令.md`「项目身份」一节用 `task_context.py save-identity` 登记。不要自己编。
5. 合同：有合同 → 告诉用户合同与排期放哪、什么格式（`.agents/skills/project-import-template`），等用户放好再导入；没有合同 → `scripts/proposal_plan.py create` 建未签约提案计划，先把阶段列给用户确认再建。
6. 用人话告诉用户：接下来说什么、你做什么；换 Codex / Claude Code 时让当前助手先保存再换边，同一项目不要两个主会话同时写；第一次检核整份看，之后只看变化页；成品只留最新 2 版，其余自动移到同级 `<项目目录名>-归档/`（只移不删）。
7. 完成后告诉用户怎么开始：“检查工作包环境和项目进度，告诉我缺什么资料。”

## 二、更新（已有工作区，无缝升级）

原则：**`project/` 是业务资料，一个文件都不改、不删、不移**；只替换工作包本身的文件；任何会动 `project/` 的事先给用户看清单。

1. 只读盘点：确认没有别的会话在写这个工作区；磁盘够放一份完整备份。`python3 scripts/project_handoff.py resume --workspace . --json` 记下当前任务、未结束分派、成果版本。把 `project/` 每个文件的路径与 SHA-256 存成升级前清单（存到工作区外）。
2. 备份：整个工作区（含隐藏文件）复制到同级 `<工作区名>-升级前备份-v<旧版本>-<日期>/`，逐文件哈希核对一致后才继续。
3. 检查本机改动：工作区里 `project/` 以外的文件与**已装版本**的发行清单（`package-inventory.json`，或该版本的 git tag 内容）逐文件比对。有差异（自己加的脚本、改过的说明）先列给用户，用户说了才覆盖。
4. 替换：用新版包里 `project/` 以外的全部内容覆盖工作区对应文件（含 `.agents`、`.claude`、`.codex`）；旧版有、新版没有的文件移到同级 `<工作区名>-升级移出-<日期>/`，不删。`__pycache__`、`.DS_Store` 可以直接移出。
5. 升级后检查并把输出给用户：
   ```bash
   python3 scripts/check_package_integrity.py --workspace . --json
   python3 scripts/sync_hosts.py check --workspace .
   python3 scripts/validate_project.py --root . --json   # errors 必须为空；成果目录里读不了的 JSON 只是 warning
   python3 scripts/project_handoff.py resume --workspace . --json
   ```
   重算 `project/` 清单，必须与升级前完全一致（只允许多出 `project/运行日志/` 下的文件）。任务、分派、旧检核报告都要读得到，下一步和升级前一致。新版读不了旧记录（格式错误、记录损坏、要求迁移）→ **停下把原始错误给用户**，不要手动改记录。
6. 版本保留试算（不执行）：对每个独立任务 `python3 scripts/banben_baoliu.py --workspace . --task-id <任务> --dry-run --json`（正式成果用 `--key <任务>::html-deck`），把“下次登记会被移到归档的旧版本、文件数、大小”汇总给用户。提醒：旧版本时期没有用 `jiaofu_guankou deliver` 登记“已发客户”的版本不受保护；列出看起来发过客户的版本问用户，**用户不确定的不要登记成已发**。
7. 告诉用户升级后的变化（第一次检核仍整份看；之后只看变化页；需要 `HARNESS_NODE_MODULES`、`pypdfium2` 的地方）与回退方法（用备份目录整个换回）。

## 三、不要做

- 不删任何文件（只移到备份或移出目录）；不改 `project/`；不在用户同意前做登记、归档、分派或建提案计划。
- 不修改其他项目、其他 Skill、MCP、密钥或宿主全局配置；本包不是全局安装包，一个项目一个目录。
- 不推送、不发布、不对外发消息、不写飞书。
- 同一个命令同类失败 2 次就停下告诉用户原因，不换花样硬试。
- 没有网络或读不到本地文件就直说，不要声称安装或更新完成。

## 四、交回

一段总结：安装/更新前后版本、工作区路径、备份与移出目录位置（更新时）、`project/` 前后清单是否一致、各项检查实际输出、环境缺项、要用户决定的事（已发客户版本、是否设 HARNESS_NODE_MODULES、合同或提案计划）。
