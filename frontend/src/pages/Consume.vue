<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <select v-model="layer">
      <option value="">全部层</option>
      <option value="upper">上层</option>
      <option value="mid">中层</option>
      <option value="lower">下层</option>
    </select>
    <input type="number" v-model.number="qty" />
    <button @click="go">FEFO 扣减</button>
    <pre>{{ result }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const items = ref([])
const item_id = ref(1)
const layer = ref('')
const qty = ref(1)
const result = ref('')
onMounted(async () => { items.value = await api('/items'); if (items.value[0]) item_id.value = items.value[0].id })
async function go() {
  try {
    const body = { item_id: item_id.value, qty: qty.value }
    if (layer.value) body.layer = layer.value  // 按层扣减：解冻转走的批不会被当原层库存扣掉
    result.value = JSON.stringify(await api('/consume', { method: 'POST', body: JSON.stringify(body) }), null, 2)
  } catch (e) { result.value = e.message }
}
</script>
