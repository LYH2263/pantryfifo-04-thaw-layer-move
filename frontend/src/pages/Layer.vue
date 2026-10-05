<template>
  <div>
    <h1>{{ label[props.layer] || props.layer }} 层</h1>
    <p v-if="err" class="err">{{ err }}</p>
    <span v-for="x in rows" :key="x.id" class="lot">
      #{{ x.id }} {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}
      <button v-if="props.layer === 'lower'" class="thaw" @click="thaw(x)">解冻转中层</button>
    </span>
    <p v-if="!rows.length" class="muted">本层暂无在架批次</p>
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
    // from_layer 乐观守卫：若该批已被并发转层/下架/消费，服务端 409，重载后以最新状态为准
    await api('/transfer', { method: 'POST', body: JSON.stringify({ lot_id: x.id, to_layer: 'mid', from_layer: 'lower' }) })
  } catch (e) {
    err.value = `批次 #${x.id} 转层失败：${e.message}`
  }
  await load()  // 成功后该批只在中层可见；失败也以服务端状态重绘本层
}
watch(() => props.layer, load)
onMounted(load)
</script>
