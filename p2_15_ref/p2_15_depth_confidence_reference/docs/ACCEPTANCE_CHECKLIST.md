# P2-15a / P2-15b 验收清单

## 单帧功能

- [ ] `depth==0` 输出 confidence 0。
- [ ] `<250 mm` 输出 confidence 0，并设置 bit1。
- [ ] `>2500 mm` 输出 confidence 0，并设置 bit1。
- [ ] `source_mask=False` 输出 confidence 0，并设置 bit5。
- [ ] 平面中心置信度高于孔洞边缘。
- [ ] 前后景突变附近设置 bit3，并显著降权。
- [ ] 3×3 单有效飞点输出 0。
- [ ] 与所有有效邻居差异超阈值的飞点输出 0，并设置 bit4。
- [ ] 小于最小面积的有效连通区被拒绝。
- [ ] bit6 始终为 0。
- [ ] bit7 始终为 0。
- [ ] 多个原因允许同置。

## 确定性和数据完整性

- [ ] 同输入、同配置连续运行两次，两张输出逐字节一致。
- [ ] `confidence_sha256` 一致。
- [ ] `reason_bits_sha256` 一致。
- [ ] 输入 depth 在调用前后逐字节一致。
- [ ] 显式全 True source mask 与省略 source mask 的输出和 input hash 一致。
- [ ] source mask 改变时 input hash 改变。
- [ ] 配置任一参数改变时 config hash 改变。

## 审计

- [ ] 每帧记录 algorithm ID。
- [ ] 每帧记录 algorithm version。
- [ ] 每帧记录 config SHA-256。
- [ ] 每帧记录 depth/source/input/output hashes。
- [ ] 每帧记录 reason bit counts。
- [ ] 每帧记录 confidence 分位数。
- [ ] 审计 JSONL 在写入后 fsync。

## 真机批量验证

- [ ] 使用 `ChunkReader` 解出原始 `uint16` 毫米 Depth。
- [ ] 抽查至少 100 帧。
- [ ] 覆盖非零比 0.86～0.90 的正常帧。
- [ ] 比较平面、孔洞、前后景边缘、细杆和飞点场景。
- [ ] 保存配置 JSON 和 SHA-256。
- [ ] 保存输入 session ID、chunk seq、frame ID。
- [ ] 保存输出 NPZ 或 MCAP hash。
- [ ] 不把 confidence 写回或覆盖原始 Depth。

## 性能和资源

- [ ] 记录目标机器单帧 p50/p95 时延。
- [ ] 记录批量处理峰值 RSS。
- [ ] 验证 `tile_rows` 改变不改变输出。
- [ ] 若优化实现，逐像素对比参考实现。
