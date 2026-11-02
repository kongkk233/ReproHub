判断下面两条内容的关系。它们来自生殖医学/辅助生殖领域的不同来源。

A：
标题：{{ a_title }}
摘要：{{ a_summary }}
来源：{{ a_source }}  时间：{{ a_date }}

B：
标题：{{ b_title }}
摘要：{{ b_summary }}
来源：{{ b_source }}  时间：{{ b_date }}

关系定义：
- same：同一项研究/同一事件的报道（例如同一论文的期刊版与新闻稿、不同媒体对同一发布的报道）
- followup：同一事件的后续进展或直接回应（评论、更正、同一课题组的延伸）
- unrelated：仅主题相近，不是同一事件

只输出 JSON：{"relation": "same|followup|unrelated", "confidence": 0-1, "reason": "一句话"}
