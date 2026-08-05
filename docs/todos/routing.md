## 路由子系统全景

```
api.cfg（.env/）
 │  ALL_MODELS: 全部模型元数据（含 conditions/TPM/RPM/RPD/thinking/effort/max_token）
 │  SUB_LIST:   子代理模型优先级列表（字符串数组）
 ▼
ModelRegistry._load()           按 SUB_LIST 顺序 -> RegistryModelSpec（严格跟随优先级）
RoutingPolicy.__init__()        持 specs + 为每个alias注册 RateLimitConfig
 │
 ├── infer_conditions()          关键词（CONDITION_KEYWORDS）+ 工具集/深度规则
 │                               → 冲突消解（simple被complex/reasoning移除）→ 空则默认simple
 ├── select_model()              条件交集 ∩ 限流 → 选alias；全限流 → 忽略条件第二轮；仍无 → RuntimeError
 └── get_fallback_chain()        运行期回退候选名单
 │
 ▼
route_request                    select_model → 重试3次 → get_fallback_chain 逐个尝试
 ▼
_get_cached_provider(alias)      线程安全缓存（_provider_cache + _cache_lock）
 ▼
_create_provider(spec.provider, spec.api_key, spec.base_url, spec.model_id,
                  thinking=spec.thinking, effort=spec.effort)
 ▼
AnthropicProvider / OpenAIProvider / GeminiProvider
```