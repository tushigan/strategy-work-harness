# 品牌屋 HTML 运行说明

本模块仅支持 macOS。可编辑 HTML 是项目工作产物；浏览器授权、原文件保存和历史恢复必须以本次实际操作结果为准。HTML 技术检查不替代正文策略确认或项目 HTML 的业务批准，未通过的业务报告不会自动改为通过。

## 使用顺序

1. 先有[策略与品牌屋运行说明](策略与品牌屋运行说明.md)所述的品牌屋正文及有效独立检核。让 Agent 生成 HTML，不把通用空白模板当项目成品。
2. 双击 `project/outputs/brand-house/<task_id>/brand-house.html` 打开。点击模型文字直接修改；“文字与版式”调整选中文字字段的字体、字号、字重、颜色、对齐和行高。
3. 点“保存原文件”，在系统选择器中选中这份HTML。成功必须显示已写回并回读确认；取消、权限拒绝、文件冲突或写入失败都不算保存成功。
4. 不支持原文件写回时，用“导出HTML”下载含完整历史的副本；原文件没有更新。确认实际下载文件后再交Agent核对，不同时拿两个分叉文件继续各自改。
5. “文件内历史”可只读预览；“恢复为新版本”会追加，不删除较新的版本。恢复后旧检核与批准不会恢复有效。
6. 回到Codex后让Agent读取实际HTML，判断改动影响。策略内容改变时先确认正文同步范围，再依次对齐下游；仅样式改变不会自动重写策略。

每次保存都包含完整文字和样式快照。支持系统无衬线、宋体/衬线、等宽、苹方和微软雅黑；缺少指定字体则按内置顺序回退，控件显示检测到的回退结果。不同操作系统不会保证逐像素相同。支持窗口为1440x1000、1024x768、390x844；文字自然增高，不保证一页纸容纳全部证据。

字号10-48px、行高1.2-2.2、列数上限1-3、最大宽度800-1600px、间距0-32px、内边距8-32px。没有任意拖拽坐标功能。

## 失败与冲突

未保存修改尽量保存在当前浏览器的辅助缓存，但缓存可能被清理、禁用或达到容量限制；不能把缓存当成文件已保存。“恢复辅助草稿”只在明确点击后载入，不自动替换磁盘历史。“草稿备份”下载JSON，含未保存文字、完整历史和冲突原文，可从“导入品牌屋文件”恢复同一文档草稿。

若检测到外部修改，本次不覆盖原文件。保留原文件及草稿备份交给Agent核对。浏览器排他写入和字节核对不能提供跨程序的绝对锁；检查与最终关闭之间仍存在极短的外部竞态，遇到未确认结果不要反复盲目覆盖。

导入读取和保存期间会暂时锁住编辑及其他改稿操作，结束后恢复。冲突核对采用原始字节，包含BOM；不能严格解码的外部内容以`bytes_base64`完整保存在草稿备份中，交Agent离线核对，不执行该内容。

主历史达到100版开始警告，300版停止新增；完整内嵌数据达到5MiB开始警告，20MiB停止新增。字节数包含分支，版本数只计主历史，不累加所有分支的版本。超过保护上限不会删除旧历史；仍可导出已有快照和未保存草稿。单个HTML读取保护为40MiB。超过范围交Agent离线处理，不能自动丢版本来腾空间。

## Agent 命令

以下命令都在当前工作包根目录执行；替换任务标识和实际作者实例。Python使用工作区配置的运行时，历史检查同时需要可用Node；可通过 `HARNESS_NODE` 指定路径，不把本机路径写入交付数据。

```sh
python scripts/brand_house.py generate --workspace . --task ITEM-001-draft --author ACTUAL-INSTANCE
python scripts/brand_house.py scan --workspace .
python scripts/brand_house.py inspect --workspace . --task ITEM-001-draft
python scripts/brand_house.py review --workspace . --task ITEM-001-draft
python scripts/brand_house.py gate --workspace . --task ITEM-001-draft
```

HTML是唯一展示内容与历史主文件；`project/records/phase4-html.json` 只存版本引用、指纹和对齐状态，不另存一份“当前品牌内容”。`last-valid.html` 是固定路径的最近有效辅助副本，不按每次保存新增一套交付文件。`html-save-log.jsonl` 区分文件内事件、Agent实际回读与浏览器回执；读取到版本不证明浏览器某次写回成功。

## 同步决定

内容手改后，先执行 `inspect --task <task_id>` 验证实际HTML。`document_id` 从本项目 `project/records/phase4-html.json` 的 `documents[task_id].document_id` 读取，`target` 和 `body` 分别取本次inspect返回的 `target` 与 `synced_body`。待对齐时review会正确阻断，不能靠先通过review来取得同步依据。

策略师明确同意把哪些当前HTML内容同步到正文后，Agent保存真实原话和原因。以下只是结构示例，不可直接当授权：

```json
{
  "action": "sync-body",
  "document_id": "本次inspect验证后从HTML登记表取得",
  "target": {"space": "html", "key": "任务标识", "revision": "当前UUID", "sha256": "实际HTML指纹"},
  "body": {"space": "phase3", "key": "任务标识::brand-house", "version": 2, "sha256": "实际正文指纹"},
  "actor": "真实策略师",
  "decision_text": "真实确认原话",
  "reason": "本次修改原因",
  "simulation": false
}
```

保存后调用 `python scripts/brand_house.py sync-body --workspace . --task ITEM-001-draft --author ACTUAL-INSTANCE --decision project/records/本次同步决定.json`。生成的新正文必须重新独立检核，随后重做受影响的推导、简报等；不沿用旧批准。若上游资料或正文也改变，不猜测合并，不覆盖人工稿。

HTML 独立报告绑定当前文件的确切字节、UUID 和完整来源，覆盖标题、11 章、缺口及问题，判断内容对应、显示、历史、保存与边界。有红灯或未覆盖必要范围就不能通过。逐字稿和 HTML 演示稿由[提案与设计检核运行说明](提案与设计检核运行说明.md)处理，本模块不替代它们的检核。

## 实现来源

原文件接口按[Chrome官方File System Access说明](https://developer.chrome.com/docs/capabilities/web-apis/file-system-access)处理授权与用户点击；关闭前暂存、关闭后回读的实现依据[MDN createWritable说明](https://developer.mozilla.org/en-US/docs/Web/API/FileSystemFileHandle/createWritable)。文档支持不等于本机验收，实际限制以当前检查结果为准。
