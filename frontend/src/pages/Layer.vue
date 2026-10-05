<template>
  <div>
    <h1>{{ label[props.layer] || props.layer }} 层</h1>
    <p v-if="err" class="err">{{ err }}</p>
    <span v-for="x in rows" :key="x.id" class="lot" :class="{ dead: x.expired }">
      {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}
      <em v-if="x.expired">已过期</em>
      <button v-if="props.layer === 'lower' && !x.expired" @click="thaw(x)">解冻→中层</button>
    </span>
  </div>
</template>
<script setup>
import { ref, watch, onMounted } from 'vue'
import { api } from '../api'
const props = defineProps({ layer: String })
const label = { upper: '上层', mid: '中层', lower: '下层' }
const rows = ref([])
const err = ref('')
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
async function thaw(x) {
  err.value = ''
  try {
    // from_layer 做乐观校验：若该批已被并发转走/下架，后端 409，这里刷新后只见最新层位
    await api('/lots/' + x.id + '/transfer', {
      method: 'POST',
      body: JSON.stringify({ to_layer: 'mid', from_layer: 'lower' }),
    })
  } catch (e) { err.value = '转层失败：' + e.message }
  await load()  // 成功后该批只在中层可见，本层列表不再出现
}
watch(() => props.layer, load)
onMounted(load)
</script>
