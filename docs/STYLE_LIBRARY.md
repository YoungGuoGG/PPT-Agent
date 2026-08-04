# PPT Agent Style Library

## 一套风格是什么

一套可用风格就是一个目录，其中 `design.md` 是唯一权威文件，`references/` 是可选的脱敏视觉参考：

```text
style-library/<style-id>/
├── design.md
└── references/*.svg
```

`design.md` 同时包含 Planner 需要的摘要字段和设计工具需要的完整规则。摘要字段包括 ID、名称、版本、标签、适用/不适用场景；正文规定定位、颜色令牌、字体、页面节奏、布局语法、图表、图片/图标和禁止事项。

## 谁读取什么

- Planner：只读取风格摘要并输出一个 `style_selection`，不决定具体颜色和坐标。
- `design.compose`：读取选中风格的完整 `design.md`，形成当前任务的设计稿与锁定规范。
- `svg.generate`：读取相同 `design.md`、锁定规范和必要的脱敏参考 SVG。
- 前端：展示已发布风格、Planner 选择原因，以及导入草稿的 Review。

## 选择优先级

1. 用户明确指定且存在的 `style_id`。
2. Planner 根据请求与 `tags`、`suitable_for`、`avoid_for` 匹配。
3. 没有明显风格信号时使用 `neutral-professional`。

每次选择都记录 `design_version` 和 `design_hash`，使同一次任务后续页面与修订可以追溯到相同设计契约。

## 从 PPTX 导入

导入器读取 OOXML 主题、字体、画布、布局几何和页面密度，并将页面转换为 SVG。进入参考库前执行以下脱敏：

- 所有文本替换为 `[TITLE n]`、`[BODY n]`、`[LABEL n]`。
- 所有图片和 Logo 替换为中性占位块。
- 移除脚本、外部链接、元数据和嵌入对象。
- 最多均匀抽取六张参考页，避免把整个源文件复制进风格包。

Agent 再根据这些安全证据编写 `design.md`。草稿通过结构校验后进入 Review；只有用户点击“确认并发布”，状态才从 `draft` 变为 `published` 并进入 Planner 目录。
