# 工作包中的提案视觉交接

命令从工作包根目录执行。路径及编号为格式示例，由总控替换为实际任务；不写入模拟批准。设计参考从本 Skill 根目录解析，运行入口从工作包根目录解析。

## 样式与来源

主控取得本任务已确认的逐字稿引用，以及其绑定的简报、品牌屋、推导引用。作者完整读取原文后，在 `project/outputs/proposal/<task_id>/visual/` 准备 UTF-8 CSS 候选及方向依据；已有方向或授权继续使用，不新增审批。

生成请求含 `script/brand-house/derivation/brief` 四个完整 `{space,key,version,sha256}` 引用和必填 `page_plan`，格式见 `../../html-deck/references/page-plan.md`。页面方案表达画面内容与引用，讲者全文自动保留；可另外提供样式字段：

```json
{
  "visual_style": {
    "path": "project/outputs/proposal/ITEM-001-proposal/visual/提案样式.css",
    "sha256": "替换为CSS文件的实际64位小写SHA256"
  }
}
```

这是字段示意，不是可单独运行的完整请求。已有演示稿再生成时还需 `revision.base/trigger/reason/changes`；与 `html-deck` 使用同一修订上限。初稿可直接加入样式，不必先登记一个无样式的基线而额外消耗修订轮次。

- CSS 必须在当前任务的提案目录中；拒绝跨任务、路径穿越、指纹不符及非 CSS 文件。
- 只支持保守样式子集，包括普通规则和 `@media/@supports`；不支持资源 URL（包括 data 图片）、导入、转义、脚本、生成内容、动画、过渡和 `!important`。这是能力限制，不是任意 CSS 安全承诺。
- 样式可改变视觉层级，但不得隐藏、截断、遮挡、换成透明文字或制造正文没有的新含义。机器安全检查不代替逐页观察。
- 正式文件始终是 `html-deck/presentation.html`。CSS 在 `style-<sha256>.css` 与 `versions/vNNNN.css` 冻结，与同版 JSON/HTML 一起进入独立检核来源。之后候选 CSS 改动不篡改历史；正式冻结文件外改则阻断。

## 登记与检查

```bash
python scripts/phase5.py deck-generate --workspace . --task ITEM-001-proposal --author ACTUAL-DECK-AUTHOR --request project/author-input/演示稿请求.json
python scripts/phase5.py deck-sources --workspace . --task ITEM-001-proposal
python scripts/phase5.py deck-browser-check --workspace . --task ITEM-001-proposal
```

对上面的实际最终文件查桌面和窄屏显示、原文、翻页、编辑草稿及无缓存重开。独立实例读取 `deck-sources` 全部原文与本次截图，按 `html-deck` 契约填写实质报告，由总控登记；随后策略师单独确认。这些动作不能由作者换个名字代办，也不能由旧版通过状态替代。

## PDF 派生副本

正式 HTML 获得有效独立报告和策略师确认后，运行：

```bash
python scripts/deck_export.py export --workspace . --key ITEM-001-proposal::html-deck
python scripts/deck_export.py inspect --workspace . --manifest project/outputs/proposal/ITEM-001-proposal/html-deck/exports/实际运行编号/manifest.json
```

使用已安装的 Mac Chrome、Node Playwright、pdf-lib 和 Python pypdf；`HARNESS_NODE` 与 `HARNESS_NODE_MODULES` 只在进程环境配置。不自动联网安装，不把开发机绝对路径写进工作包。缺依赖保留失败和待导出状态。

每次导出使用新目录，清单绑定正式 HTML 引用、当前文件指纹、报告和确认记录、导出器、页面及 PDF 字节。导出前后重新查来源，失效或并发变化即失败。再次检查不会给 PDF 添加业务批准。

导出成功仍为 `exported_pending_visual_review`。总控实际回读 PDF 页数、全部原文并渲染每页，交另一名独立实例对照最终 HTML 检查字体、行距、截断、层级、页面尺寸和顺序，保留绑定清单与 PDF 指纹的报告。没有 PDF 逐页证据就只交待检副本；HTML 的草稿导出重开测试不能冒充 PDF 检查。正式对外使用前重新 `inspect`，并核实策略师授权与真实交付要求。

## 扩展路线

本版不把自由布局多文件、概览墙或 PPTX 声称为已自动受控。本地静态图片通过 page_plan.image 登记并冻结，不使用 CSS URL。明确要求其他扩展时，先说明能力边界和交付方式；候选保存在本任务内，不覆盖正式 HTML。上游路线 A 需要从头按可编辑约束写 HTML，路线 B 需要 Python Playwright 等独立依赖；必须实际逐页转换、回读、渲染、检核，不能继承另一份 HTML 的批准。需改变正式工作包结构或门禁时，回到维护工作，不在客户项目中临时绕过。
