# 外部矢量接口（预留）

geo-icon 只用圆和直线构造图标。龙、麒麟、凤凰这类题材做不好，Rikyū 遇到它们也不走几何路线，
而是交给自由矢量模型（它用的是 QuiverAI Arrow），而且不出构图卡。

本 skill **不内置任何第三方服务**，只预留接口。需要时按下面的方式接入。

## 什么时候走外部

先尝试简化：把题材拆成 10 个以内的几何部件（头、身体、四肢、一个特征符号）。能拆就用构造模式。
拆不了、用户又坚持要细节丰富的版本，才走外部。

## 接入方式

在 `scripts/external.py` 里写一个函数并注册：

```python
def my_provider(brief: str, references: list, out_path: str) -> str:
    key = os.environ["MY_PROVIDER_API_KEY"]      # 密钥只放环境变量
    # 调用服务的文生 SVG 接口，把返回的 SVG 写到 out_path
    ...
    return out_path

PROVIDERS = {"my": my_provider}
```

然后：

```bash
export GEO_ICON_PROVIDER=my
python3 scripts/external.py --brief "圆形徽章，盘龙，黑白两色，无文字" -o dragon.svg
python3 scripts/geoicon.py --svg dragon.svg -o review/
```

## brief 怎么写

只写用户提出的事实和要求：题材、构图、颜色、不要什么。不要替用户加没提过的图案或风格，
这也是 Rikyū 总控 agent 的做法。比如：

> 圆形徽章图标，中国龙盘绕于圆环内，龙首昂起张口，黑白两色，扁平矢量，无文字、无渐变、无复杂背景。
> 要求缩小到 32px 仍能辨认：线条粗、细节少。

## 评审

`geoicon.py --svg` 输出 `poster.png`、`sizes.png` 和 `report.txt`。重点看两项：
- `sizes.png`：32px 以下能否辨认。复杂题材最容易在这里失败。
- `report.txt` 的 `thin parts` / `tight gaps`：数量多说明缩小后会糊。
  实测 Arrow 生成的龙：20 处过细、23 处缝太窄，64px 还能看，32px 以下糊成一团。

不合格就把具体问题写进下一轮 brief（"去掉鳞片"、"线条加粗到至少 X"），重新生成。

## 可选的服务（截至调研时）

| 服务 | 说明 |
|---|---|
| QuiverAI Arrow | Rikyū 实际使用的模型。`POST https://api.quiver.ai/v1/svgs/generations`，按 token 计费，一次约 $0.08–0.12 |
| Recraft vector | 原生输出 SVG 的模型（V4 / V4.1 Vector），OpenRouter 上约每张 $0.08 |
| 图像模型 + 矢量化 | GPT image 等出黑白图，再用 VTracer / potrace / Vectorizer.AI 转成 SVG；路径不如原生 SVG 模型干净 |

价格和接口以各服务官方文档为准。
