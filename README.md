python main.py live \
  --url "https://polymarket.com/event/will-bitcoin-hit-80k-or-100k-first-272" \
  --url "https://polymarket.com/event/will-bitcoin-hit-80k-or-150k-first" \
  --outcome "100k" \
  --stake 100 \
  --target_plus 10 \
  --interval 10 \
  --both \
  --graph \
  --png_prefix "graphs/graph"

 ^
 |
 |
 |
(put this in terminal)


Как работает graph-mode:
- Собирает точки бесконечно
- Ctrl+C ОДИН раз  -> сохранит PNG для КАЖДОГО URL и продолжит
- Ctrl+C ДВА раза  -> выйдет


Ctrl+C once  -> save PNGs and CONTINUE
Ctrl+C twice -> exit
