# DF 攻略插件（图+文模块注入版）

## 我能做什么？
- **你自己**在 `packs/df/entries/` 下新增或编辑条目：
  - 支持 **Markdown + YAML 前言**（推荐），可在正文中**任意位置**插入图片：`![](相对路径或URL)`；
  - 或者写 JSON：顺序化 `blocks`（`text` / `image`）。
- 发送 `/df <关键词>` 时，**直接输出**你写好的**文字+图片**（顺序保持）。

## 目录结构
```
astrbot_plugin_gameguide/
  main.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
  packs/
    df/
      entries/
        水壶.md
        奇异心情.md
      assets/
        waterskin.png
```
> 你也可以自定义 `pack_dir` 到任意路径（相对插件目录）。

## Markdown 条目示例（packs/df/entries/水壶.md）
```markdown
---
key: "水壶"
aliases: ["waterskin", "flask", "酒壶"]
images: []   # 可选：额外在文末追加的图片列表
---
**用途**：用于携带饮水，远征或荒野探索常备。

制作建议：
- 材料：皮革/金属；
- 工坊：皮革工或金属工坊。

![](waterskin.png)

**提示**：耐久与容量随材料而变，建议在军团出征前人手一件。
```
> 插图路径相对 `entries/` 或 `assets/` 均可；也可填 `https://...`。

## JSON 条目示例（packs/df/entries/waterskin.json）
```json
{
  "key": "水壶",
  "aliases": ["waterskin", "flask", "酒壶"],
  "blocks": [
    {"type":"text","content":"用于携带饮水，远征必备。"},
    {"type":"image","src":"assets/waterskin.png"},
    {"type":"text","content":"制作：皮革工/金属工坊。"}
  ]
}
```

## 常用指令
- `/df 水壶` → 输出你写的图+文
- `/df.kb 量子仓库` → 仅查内容包/KB
- `/df.wiki embark` → 仅查 Wiki
- `/df.list` → 列出所有条目；`/df.list 水` → 按前缀过滤
- `/df.sync` → 改完文件后热加载

## LLM 函数工具
- `kb_lookup(keyword)` → 返回 `{found, text, images[]}`（给模型用；会返回图片 URL 或相对路径名）
- `wiki_search(query, title?, limit?)` → DF wiki 摘要 + 链接

## 依赖
- `requests`
- `PyYAML`（用于解析 YAML 前言）
