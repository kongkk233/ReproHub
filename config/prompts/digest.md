你是生殖医学前沿{{ kind_label }}的主编。下面是本期（{{ period_label }}）由规则编纂的入选条目（按主题分栏，每条含分数与一句话摘要）。请写：
1. overview：总述，200-350 字，概括本期最重要的 3-5 个进展与趋势（点名具体研究），避免空话；
2. sections：每个栏目一句导读（≤60 字），指出该栏目最值得看的 1-2 条及原因。

【本期条目】
{% for sec in sections %}
## {{ sec.topic_name }}（{{ sec['items']|length }} 条）
{% for it in sec['items'] %}
- [{{ it.final_score }}分] {{ it.title_zh or it.title }} —— {{ it.summary_zh or it.summary }}（{{ it.source_name }}）
{% endfor %}
{% endfor %}

只输出 JSON：
{"overview": "……", "sections": [{"topic": "主题 slug", "lead": "导读"}]}
主题 slug 顺序：{{ section_slugs }}
