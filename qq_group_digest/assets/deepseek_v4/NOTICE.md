# DeepSeek V4 tokenizer data

These two JSON files are unmodified data from DeepSeek's official offline tokenizer package, downloaded on 2026-09-25. The plugin uses the tokenizer data through Hugging Face `tokenizers`; it does not run the package's Python example or fetch anything at runtime.

- Documentation: https://api-docs.deepseek.com/quick_start/token_usage
- Original archive: https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip
- Archive SHA-256: `e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495`
- `tokenizer.json` SHA-256: `89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614`
- `tokenizer_config.json` SHA-256: `841f8cf146e3f0ad1082594a31f68ecf7608c20467ef355081333d85bbaeb1cb`

ECNU currently maps `ecnu-max` to DeepSeek-V4-Flash-0731: https://developer.ecnu.edu.cn/vitepress/llm/model.html

The tokenizer configuration is retained for provenance. Its `model_max_length` is not the service context limit. ECNU's documented 512K context and the plugin's explicit reservations determine the input budget. Counts use `add_special_tokens=False`, with truncation and padding disabled; conversation formatting and output have separate allowances. API `usage` remains authoritative for billing.
