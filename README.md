# Бункер онлайн
Локально: `pip install -r requirements.txt`, затем `python server.py` и открыть http://localhost:8080

Хостинг (Render, Railway, Fly и т.п.): Python-сервис.
- Build: `pip install -r requirements.txt`
- Start: `python server.py`
- Порт берётся из переменной PORT, WebSocket на пути /ws, за HTTPS работает через wss автоматически.

Комнаты хранятся в памяти и пропадают при перезапуске сервера. Паки лежат в браузере ведущего и отправляются в комнату при её создании.
