# astrbot_plugin_df

**/df 关键词** → 发送你预设的图文（本地 JSON 数据），不调用任何 LLM。  
匹配顺序：**精确 → 别名 → 前缀/包含 → 模糊(difflib)**。

## 目录
```
astrbot_plugin_df-master/
  main.py
  dfpkg/
    __init__.py
    utils.py
    catalog.py
    repos.py
    resolver.py
  catalog/aliases.json
  texts/texts.json
  images/images.json
  assets/df_placeholder.png
  requirements.txt
```

### 数据文件
- `catalog/aliases.json`
```json
{ "aliases": { "皮水壶": "水壶", "waterskin": "水壶" } }
```
- `texts/texts.json`
```json
{ "entries": { "水壶": "这是你预设的文本答案（示例）。" } }
```
- `images/images.json`
```json
{ "entries": { "水壶": ["assets/df_placeholder.png"] } }
```

## 指令
- `/df 关键词`：返回图文（存在则发图片和/或文字）或“没有答案”
- `/df.sync`：重载三类数据文件
- `/df.cat`：查看别名目录（最多50条）

> 需要禁止“命令消息转发到模型”，并启用 `/` 命令前缀。
