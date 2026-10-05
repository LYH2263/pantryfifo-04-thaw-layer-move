# Pantryfifo · 冰箱临期先吃

分批入库 → FEFO 扣减 → 过期下架。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5300 |
| API | 10300 |

0-1：`shopping_list` / `recipe_suggest`。

`temp_zone` 解冻转层：批级层位覆盖（`lots.layer_override`，NULL 跟随品项默认层），
单批下层→中层 `POST /api/transfer {lot_id, to_layer, from_layer?}`，与消费/过期扫描
同一写事务串行；过期判定与顶条、下架名单共用 `engines.fefo.is_expired`。
