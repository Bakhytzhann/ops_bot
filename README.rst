OPS Bot Telegram Mini App
=========================

Open **OPS** from the bot menu to see the six operations tools. In **SKED**,
select a flight to open its card and **Preparation**.
Leon provides the schedule, crew, duty, and handling. ForeFlight provides the
flight plan and briefing. Weather uses Leon and AWC.

Backend
-------

Use Python 3.12 and the existing virtual environment. FastAPI and the polling
bot use the same local configuration and SQLite access list. For a new
checkout, use `.env.example`_ to create a local ``.env``.

.. code-block:: powershell

   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   if ($env:DEBUG -eq 'release') { Remove-Item Env:DEBUG }
   .\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000

Check ``http://127.0.0.1:8000/health`` for ``{"status":"ok"}``.

Frontend
--------

From the repo root, install and build the React frontend with Node.js 22.12
or newer. FastAPI serves the built app at ``http://127.0.0.1:8000/``. Build
before starting FastAPI for the first time.

.. code-block:: powershell

   npm.cmd --prefix .\frontend ci
   npm.cmd --prefix .\frontend run build

For frontend development, ``npm.cmd --prefix .\frontend run dev`` starts Vite
with a local proxy to FastAPI. The page prompts users to open **OPS** in
Telegram when opened directly in a browser.

Telegram test with Cloudflare Quick Tunnel
------------------------------------------

1. With FastAPI running, install `cloudflared for Windows`_ and open a
   separate PowerShell window.

   .. code-block:: powershell

      cloudflared tunnel --url http://127.0.0.1:8000

2. Copy the temporary ``https://…trycloudflare.com`` URL. In **@BotFather**,
   use ``/setmenubutton`` for the bot that matches the backend configuration.
   Set the URL and name the button **OPS**. For a persistent HTTPS address,
   you can set ``TELEGRAM_MINI_APP_URL`` locally and start the bot with
   ``main.py`` to register the menu.
3. From an approved Telegram account, open **OPS** in the private bot chat.
   Open **SKED** and pick a flight present in both Leon and ForeFlight. Open
   **Preparation** and check **CREW / FDP**, **WX / TAF**, and **NOTAM**.
4. Stop the `Quick Tunnel`_ with **Ctrl+C** when the test is complete.

See `MINI_APP.md`_ for more about access and the API contract.

.. _.env.example: .env.example
.. _MINI_APP.md: MINI_APP.md
.. _cloudflared for Windows: https://developers.cloudflare.com/tunnel/downloads/#windows
.. _Quick Tunnel: https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/
