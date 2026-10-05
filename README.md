# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架 → 解冻转层（批级层位覆盖，`temp_zone`）。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest`；`temp_zone` 已实现：批级 `layer_override` + 条件 UPDATE 转层，过期判定与下架、顶条同一套（`is_expired`）。
