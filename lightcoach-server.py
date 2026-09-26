#!/usr/bin/env python3
"""
LightCoach Backend Server
AI crypto trading coach powered by Lightchain AIVM.
Subscription model: users pay LCAI monthly to unlock the Coach tab.

Deploy to Railway:
  - Set LIGHTCHAIN_PRIVATE_KEY (dApp wallet private key — pays AIVM fees)
  - Set DATA_DIR to your Railway volume mount (e.g. /data)
  - Railway injects PORT automatically

Requirements: flask, flask-cors, requests, web3==7.6.0, eth-account==0.13.7,
              cryptography, websocket-client, gunicorn
"""

import json, os, time, threading, base64 as _b64_mod, secrets as _secrets_mod
import urllib.request
import sqlite3
from flask import Flask, request, jsonify
from flask_cors import CORS

# ── CONFIG ─────────────────────────────────────────────────────────────────────
PORT              = int(os.environ.get("PORT", 8188))
LCAI_RPC          = "https://rpc.mainnet.lightchain.ai"
OWNER_WALLET      = "0x6518fd26a7ad2fe1ba80de5f279ee59f55c0a9ba"   # MetaMask — receives subscription fees
MONTHLY_PRICE_USD = 1.00   # $1 / 30 days

PREMIUM_WHITELIST = {
    "0xa3a653a8cba0710ff57ac34e2278c603b4259fd3",   # Main Trust Wallet
    "0x6518fd26a7ad2fe1ba80de5f279ee59f55c0a9ba",   # MetaMask
    "0x729fea1d8ca343f26c4cc743a4e1898d65ce6a76",   # dApp Wallet
}

PRIVATE_KEY = os.environ.get("LIGHTCHAIN_PRIVATE_KEY", "").strip()
# Prefer Railway volume mount (/data); fall back to ~/ if unwritable
_DATA_DIR_RAW = os.environ.get("DATA_DIR", "/data")
try:
    os.makedirs(_DATA_DIR_RAW, exist_ok=True)
    _probe = os.path.join(_DATA_DIR_RAW, ".write_probe")
    with open(_probe, "w") as _f:
        _f.write("ok")
    os.remove(_probe)
    DATA_DIR = _DATA_DIR_RAW
except Exception:
    DATA_DIR = os.path.expanduser("~")
    print(f"[lightcoach] DATA_DIR {_DATA_DIR_RAW!r} not writable — using {DATA_DIR}", flush=True)
DB_FILE     = os.path.join(DATA_DIR, "lightcoach.db")

# ── AIVM CONFIG ────────────────────────────────────────────────────────────────
AIVM_GATEWAY  = "https://chat-api.mainnet.lightchain.ai"
AIVM_RELAY    = "wss://relay.mainnet.lightchain.ai/ws"
AIVM_JOB_REG  = "0xfB15F90298e4CcD7106E76fFB5e520315cC42B0b"
AIVM_JOB_FEE  = 20_000_000_000_000_000   # 0.02 LCAI in wei
AIVM_CHAIN_ID = 9200

AIVM_ABI = [
    {
        "name": "createSession", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "paramsHash",      "type": "bytes32"},
            {"name": "worker",          "type": "address"},
            {"name": "encWorkerKey",    "type": "bytes"},
            {"name": "ephemeralPubKey", "type": "bytes"},
            {"name": "initState",       "type": "bytes"},
            {"name": "expiry",          "type": "uint256"},
        ],
        "outputs": [{"name": "sessionId", "type": "uint256"}],
    },
    {
        "name": "submitJob", "type": "function", "stateMutability": "payable",
        "inputs": [
            {"name": "sessionId",  "type": "uint256"},
            {"name": "promptHash", "type": "bytes32"},
        ],
        "outputs": [{"name": "jobId", "type": "uint256"}],
    },
    {
        "anonymous": False, "name": "SessionCreated", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "sessionId",      "type": "uint256"},
            {"indexed": True,  "name": "user",            "type": "address"},
            {"indexed": True,  "name": "paramsHash",      "type": "bytes32"},
            {"indexed": False, "name": "worker",          "type": "address"},
            {"indexed": False, "name": "encWorkerKey",    "type": "bytes"},
            {"indexed": False, "name": "ephemeralPubKey", "type": "bytes"},
        ],
    },
    {
        "anonymous": False, "name": "JobCompleted", "type": "event",
        "inputs": [
            {"indexed": True,  "name": "jobId",         "type": "uint256"},
            {"indexed": True,  "name": "worker",         "type": "address"},
            {"indexed": False, "name": "responseHash",   "type": "bytes32"},
            {"indexed": False, "name": "ciphertextHash", "type": "bytes32"},
        ],
    },
]

# ── SYSTEM PROMPT ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are LightCoach, a friendly and knowledgeable AI crypto trading coach. You are built on Lightchain — a next-generation blockchain network — and you help people learn how to trade cryptocurrency through a paper trading simulator (virtual money only — no real funds are ever at risk).

YOUR ROLE:
- Teach crypto and trading concepts in plain, simple language
- Help users understand their simulated trades and what they mean
- Explain strategies: HODL, day trading, diversification, risk management, stop-losses, DCA
- Break down market terms, chart patterns, and crypto basics simply
- Give personalized advice based on the user's goals and experience level
- Be encouraging and patient — trading is complex and learning takes time

STRICT RULES:
- NEVER tell users to buy or sell specific assets with real money
- ALWAYS be clear that LightCoach is an educational simulator — all trades are virtual
- Keep responses SHORT and mobile-friendly (under 200 words unless depth is needed)
- Use clear language, bullet points, and simple English
- If user context is provided below, use it to personalize your response

You are upbeat, patient, and love teaching. Your tone is like a smart friend who trades crypto — casual but informative."""


# ── DATABASE ───────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            wallet     TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            tx_hash    TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    # Cloud save so users can resume their paper-trading progress on any device.
    # Stores only the user's own game state (portfolios, notes, stats), keyed to their wallet.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            wallet     TEXT PRIMARY KEY,
            state      TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


# ── LCAI PRICE ─────────────────────────────────────────────────────────────────
_price_cache = {'price': 0.004, 'ts': 0}

def get_lcai_price():
    global _price_cache
    now = time.time()
    if now - _price_cache['ts'] < 300:
        return _price_cache['price']
    sources = [
        (
            "https://api.geckoterminal.com/api/v2/simple/networks/lightchain/token_price/0xa5e2ef3f4c6ac54c5e6e07edcabb0c5a6b4fc7e9",
            lambda d: float(list(d["data"]["attributes"]["token_prices"].values())[0])
        ),
        (
            "https://api.dexscreener.com/latest/dex/tokens/0xa5e2ef3f4c6ac54c5e6e07edcabb0c5a6b4fc7e9",
            lambda d: float(d["pairs"][0]["priceUsd"])
        ),
    ]
    for url, extract in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LightCoach/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                price = extract(json.loads(r.read()))
                if price and price > 0:
                    _price_cache = {'price': price, 'ts': now}
                    return price
        except Exception:
            pass
    _price_cache['ts'] = now
    return _price_cache['price']


# ── RPC HELPER ─────────────────────────────────────────────────────────────────
def lightchain_rpc(method, params):
    payload = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1}).encode()
    req = urllib.request.Request(
        LCAI_RPC, data=payload, method='POST',
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── AIVM HELPERS ───────────────────────────────────────────────────────────────
def _decode_pubkey(s):
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    s = s.strip()
    if s.startswith(('0x', '0X')):
        b = bytes.fromhex(s[2:])
    elif len(s) == 130 and all(c in '0123456789abcdefABCDEF' for c in s):
        b = bytes.fromhex(s)
    else:
        b = _b64_mod.b64decode(s)
    if len(b) != 65:
        raise ValueError(f"pubkey: expected 65 bytes, got {len(b)}")
    return b

def _ecdh_wrap(session_key, peer_pub_bytes):
    from cryptography.hazmat.primitives.asymmetric.ec import (
        generate_private_key, ECDH, EllipticCurvePublicNumbers, SECP256R1
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.backends import default_backend
    x = int.from_bytes(peer_pub_bytes[1:33], 'big')
    y = int.from_bytes(peer_pub_bytes[33:65], 'big')
    peer_pub   = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key(default_backend())
    ephem_priv = generate_private_key(SECP256R1(), default_backend())
    shared     = ephem_priv.exchange(ECDH(), peer_pub)
    pub_nums   = ephem_priv.public_key().public_numbers()
    ephem_pub  = (b'\x04' + pub_nums.x.to_bytes(32, 'big') + pub_nums.y.to_bytes(32, 'big'))
    nonce      = _secrets_mod.token_bytes(12)
    ct_tag     = AESGCM(shared).encrypt(nonce, session_key, None)
    return ephem_pub + nonce + ct_tag

def _aes_encrypt(key, plaintext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = _secrets_mod.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)

def _aes_decrypt(key, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < 28:
        raise ValueError("ciphertext too short")
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


# ── AIVM CLIENT ────────────────────────────────────────────────────────────────
class AIVMClient:
    """Server-side Lightchain AIVM inference. No user wallet required."""

    def __init__(self, private_key):
        import requests as _req
        from web3 import Web3
        from eth_account import Account
        self._req      = _req
        self._w3       = Web3(Web3.HTTPProvider(LCAI_RPC))
        self._account  = Account.from_key(private_key)
        self._registry = self._w3.eth.contract(
            address=Web3.to_checksum_address(AIVM_JOB_REG), abi=AIVM_ABI)
        self._jwt      = None
        self._jwt_exp  = 0

    def _get_jwt(self):
        from eth_account.messages import encode_defunct
        if self._jwt and time.time() < self._jwt_exp - 30:
            return self._jwt
        r = self._req.get(
            f"{AIVM_GATEWAY}/api/auth/challenge",
            params={"address": self._account.address}, timeout=15,
        )
        r.raise_for_status()
        resp = r.json()
        message = resp.get("message") or resp.get("nonce") or list(resp.values())[0]
        sig = self._account.sign_message(encode_defunct(text=message))
        r2 = self._req.post(
            f"{AIVM_GATEWAY}/api/auth/verify",
            json={"message": message, "signature": "0x" + sig.signature.hex()},
            timeout=15,
        )
        r2.raise_for_status()
        v = r2.json()
        self._jwt = v["token"]
        exp_str = v["expiresAt"][:19].replace("T", " ")
        self._jwt_exp = time.mktime(time.strptime(exp_str, "%Y-%m-%d %H:%M:%S"))
        print(f"  [AIVM] JWT obtained, expires={exp_str}")
        return self._jwt

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        }

    def run_inference(self, prompt, timeout_secs=300):
        import websocket as _ws
        from web3 import Web3
        from urllib.parse import quote as _quote

        req = self._req
        print(f"  [AIVM] starting inference ({len(prompt)} chars)")

        r = req.get(f"{AIVM_GATEWAY}/api/models", timeout=15)
        r.raise_for_status()
        models = r.json().get("models", [])
        # Prefer the upgraded coach model; fall back to llama3-8b, then whatever's available.
        # Model coverage on Mainnet is thin for big models — the fallback keeps Coach answering
        # even if no worker is currently serving the preferred model.
        _prefs = ["qwen3.6:27b", "qwen3.6-27b", "llama3-8b"]
        model = None
        for _p in _prefs:
            model = next((m for m in models if m.get("name") == _p), None)
            if model:
                break
        if not model:
            model = models[0] if models else None
        if not model:
            raise RuntimeError("No AIVM models available")
        model_id = model["id"]
        print(f"  [AIVM] model: {model['name']} id={model_id[:10]}...")

        r = req.post(
            f"{AIVM_GATEWAY}/api/sessions/select",
            json={"modelId": model_id},
            headers=self._headers(), timeout=15,
        )
        r.raise_for_status()
        sel = r.json()
        print(f"  [AIVM] worker: {sel['worker']}")

        session_key  = _secrets_mod.token_bytes(32)
        enc_worker   = _ecdh_wrap(session_key, _decode_pubkey(sel["workerEncryptionKey"]))
        enc_disputer = _ecdh_wrap(session_key, _decode_pubkey(sel["disputerEncryptionKey"]))

        r = req.post(
            f"{AIVM_GATEWAY}/api/sessions/prepare",
            json={
                "modelId":        model_id,
                "encWorkerKey":   _b64_mod.b64encode(enc_worker).decode(),
                "encDisputerKey": _b64_mod.b64encode(enc_disputer).decode(),
            },
            headers=self._headers(), timeout=15,
        )
        r.raise_for_status()
        prep = r.json()

        params_hash = bytes.fromhex(model_id[2:].zfill(64) if model_id[:2].lower() == "0x" else model_id.zfill(64))
        sig_bytes   = bytes.fromhex(prep["signature"][2:] if prep["signature"][:2].lower() == "0x" else prep["signature"])
        gas_price   = self._w3.eth.gas_price
        nonce_val   = self._w3.eth.get_transaction_count(self._account.address)

        tx = self._registry.functions.createSession(
            params_hash,
            Web3.to_checksum_address(prep["worker"]),
            enc_worker, enc_disputer, sig_bytes, prep["expiry"],
        ).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce_val,
            "gas":      1_000_000,
            "gasPrice": gas_price,
            "value":    0,
            "chainId":  AIVM_CHAIN_ID,
        })
        signed   = self._account.sign_transaction(tx)
        tx_hash  = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  [AIVM] createSession tx: {tx_hash.hex()}")
        receipt1 = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt1.status != 1:
            raise RuntimeError("createSession reverted on-chain")

        session_id = None
        for log in receipt1.logs:
            try:
                evt = self._registry.events.SessionCreated().process_log(log)
                session_id = evt["args"]["sessionId"]
                break
            except Exception:
                pass
        if session_id is None:
            raise RuntimeError("SessionCreated event not found in receipt")
        print(f"  [AIVM] sessionId: {session_id}")

        relay_token = None
        deadline    = time.time() + 60
        while time.time() < deadline:
            r = req.get(
                f"{AIVM_GATEWAY}/api/sessions/{session_id}/token",
                headers=self._headers(), timeout=10,
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("token"):
                    relay_token = d["token"]
                    break
            time.sleep(1)
        if not relay_token:
            raise RuntimeError("Relay token not ready within 60s")

        chunks   = []
        ws_ready = threading.Event()
        ws_err   = [None]

        def _on_message(ws_obj, msg):
            try:
                frame = json.loads(msg)
                payload = frame.get("payload")
                if payload:
                    blob = _b64_mod.b64decode(payload)
                    pt   = _aes_decrypt(session_key, blob)
                    chunks.append(pt.decode("utf-8", errors="replace"))
            except Exception:
                pass

        def _on_open(ws_obj):    ws_ready.set()
        def _on_error(ws_obj, e): ws_err[0] = e; ws_ready.set()

        ws = _ws.WebSocketApp(
            f"{AIVM_RELAY}?token={_quote(relay_token)}",
            on_message=_on_message, on_open=_on_open, on_error=_on_error,
        )
        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()
        ws_ready.wait(timeout=15)
        if ws_err[0]:
            raise RuntimeError(f"WebSocket failed: {ws_err[0]}")
        print("  [AIVM] relay connected")

        cipher = _aes_encrypt(session_key, prompt.encode("utf-8"))
        r = req.post(
            f"{AIVM_GATEWAY}/api/blobs",
            json={"data": _b64_mod.b64encode(cipher).decode()},
            headers=self._headers(), timeout=15,
        )
        r.raise_for_status()
        blob_hashes = r.json().get("blobHashes", [])
        if not blob_hashes:
            raise RuntimeError("No blob hash returned from gateway")
        _bh = blob_hashes[0]
        prompt_hash = bytes.fromhex(_bh[2:].zfill(64) if _bh[:2].lower() == "0x" else _bh.zfill(64))

        nonce_val2 = self._w3.eth.get_transaction_count(self._account.address)
        tx2 = self._registry.functions.submitJob(session_id, prompt_hash).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce_val2,
            "gas":      500_000,
            "gasPrice": gas_price,
            "value":    AIVM_JOB_FEE,
            "chainId":  AIVM_CHAIN_ID,
        })
        signed2  = self._account.sign_transaction(tx2)
        tx_hash2 = self._w3.eth.send_raw_transaction(signed2.raw_transaction)
        print(f"  [AIVM] submitJob tx: {tx_hash2.hex()}")
        receipt2 = self._w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=90)
        if receipt2.status != 1:
            raise RuntimeError("submitJob reverted — check LCAI balance on dApp wallet")

        job_completed_topic = "0x" + Web3.keccak(
            text="JobCompleted(uint256,address,bytes32,bytes32)"
        ).hex()

        done     = False
        deadline = time.time() + timeout_secs
        while time.time() < deadline and not done:
            time.sleep(5)
            if chunks:
                done = True
                break
            try:
                head = self._w3.eth.block_number
                logs = self._w3.eth.get_logs({
                    "address":   Web3.to_checksum_address(AIVM_JOB_REG),
                    "fromBlock": receipt2.blockNumber,
                    "toBlock":   head,
                    "topics":    [job_completed_topic],
                })
                if logs:
                    done = True
            except Exception as e:
                print(f"  [AIVM] log poll error: {e}")

        time.sleep(3)
        ws.close()
        result = "".join(chunks).strip()
        if not result and not done:
            raise RuntimeError(f"Timeout after {timeout_secs}s waiting for AIVM response")
        return result or "No response from AIVM worker. Please try again."


_aivm_client = None

def _get_aivm_client():
    global _aivm_client
    if _aivm_client is None:
        if not PRIVATE_KEY:
            raise RuntimeError("LIGHTCHAIN_PRIVATE_KEY not set")
        _aivm_client = AIVMClient(PRIVATE_KEY)
    return _aivm_client


# ── ASYNC JOBS ─────────────────────────────────────────────────────────────────
_jobs      = {}
_jobs_lock = threading.Lock()

def _start_job(message, context=""):
    job_id = _secrets_mod.token_hex(8)
    with _jobs_lock:
        _jobs[job_id] = {'status': 'pending', 'reply': None, 'error': None}

    def _run():
        try:
            full_prompt = SYSTEM_PROMPT
            if context:
                full_prompt += f"\n\n[USER CONTEXT]\n{context}"
            full_prompt += f"\n\nUser: {message}\nLightCoach:"
            reply = _get_aivm_client().run_inference(full_prompt)
            with _jobs_lock:
                _jobs[job_id].update({'status': 'done', 'reply': reply})
        except Exception as e:
            print(f"[AIVM ERROR] {e}")
            with _jobs_lock:
                _jobs[job_id].update({'status': 'error', 'error': str(e)[:300]})

    threading.Thread(target=_run, daemon=True).start()
    return job_id


# ── FLASK APP ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
# Scoped CORS — override with CORS_ORIGINS env (comma-separated)
_CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS",
    "https://lightcoach.win,https://www.lightcoach.win,https://lightcoach-production.up.railway.app,http://localhost:8188,http://127.0.0.1:8188"
).split(",") if o.strip()]
CORS(app, origins=_CORS_ORIGINS)


@app.route('/api/health')
def api_health():
    return jsonify({
        'status':  'ok',
        'service': 'lightcoach',
        'aivm':    'ready' if PRIVATE_KEY else 'no key',
    })


@app.route('/api/lcai-price')
def api_lcai_price():
    price = get_lcai_price()
    required_lcai = round(MONTHLY_PRICE_USD / price, 2)
    return jsonify({
        'price_usd':      price,
        'required_lcai':  required_lcai,
        'monthly_usd':    MONTHLY_PRICE_USD,
        'owner_wallet':   OWNER_WALLET,
    })


TRIAL_DAYS = 7   # one-time free trial per wallet, then $1/month


@app.route('/api/lc/subscription/<wallet>')
def api_subscription(wallet):
    w = wallet.lower().strip()
    if w in PREMIUM_WHITELIST:
        return jsonify({'subscribed': True, 'expires_at': None, 'whitelisted': True})
    now  = int(time.time())
    conn = get_db()
    row  = conn.execute('SELECT expires_at, tx_hash FROM subscriptions WHERE wallet = ?', (w,)).fetchone()

    # First time this wallet has ever connected → grant a one-time free trial.
    # The row persists after it lapses, so a wallet can never re-trial by reconnecting.
    if row is None and _valid_wallet(w):
        trial_expires = now + TRIAL_DAYS * 24 * 60 * 60
        conn.execute(
            'INSERT OR IGNORE INTO subscriptions (wallet, expires_at, tx_hash) VALUES (?, ?, ?)',
            (w, trial_expires, 'trial')
        )
        conn.commit()
        conn.close()
        return jsonify({
            'subscribed': True,
            'expires_at': trial_expires,
            'trial':      True,
        })

    conn.close()
    subscribed = bool(row and row['expires_at'] and row['expires_at'] > now)
    is_trial   = bool(subscribed and row and row['tx_hash'] == 'trial')
    return jsonify({
        'subscribed': subscribed,
        'expires_at': row['expires_at'] if row else None,
        'trial':      is_trial,
    })


@app.route('/api/lc/verify-subscription', methods=['POST'])
def api_verify_subscription():
    data    = request.json or {}
    w       = (data.get('wallet') or '').lower().strip()
    tx_hash = (data.get('tx_hash') or '').strip()
    if not w or not tx_hash:
        return jsonify({'error': 'wallet and tx_hash required'}), 400
    if w in PREMIUM_WHITELIST:
        return jsonify({'success': True, 'subscribed': True, 'whitelisted': True})
    try:
        result = lightchain_rpc('eth_getTransactionByHash', [tx_hash])
        tx = result.get('result')
        if not tx:
            return jsonify({'error': 'Transaction not found. Wait a moment and try again.'}), 404
        to_addr = (tx.get('to') or '').lower()
        if to_addr != OWNER_WALLET:
            return jsonify({'error': 'Payment not sent to LightCoach subscription address.'}), 400
        price         = get_lcai_price()
        required_lcai = MONTHLY_PRICE_USD / price
        required_wei  = int(required_lcai * 1e18 * 0.95)   # 5% tolerance
        tx_value      = int(tx.get('value', '0x0'), 16)
        if tx_value < required_wei:
            sent   = round(tx_value / 1e18, 4)
            needed = round(required_lcai, 2)
            return jsonify({'error': f'Amount too low — sent {sent} LCAI, needed ~{needed} LCAI'}), 400
        try:
            rcpt = lightchain_rpc('eth_getTransactionReceipt', [tx_hash]).get('result')
            if rcpt and rcpt.get('status') == '0x0':
                return jsonify({'error': 'Transaction failed on-chain. Please send a new one.'}), 400
        except Exception:
            pass
        expires_at = int(time.time()) + 30 * 24 * 60 * 60
        conn = get_db()
        conn.execute(
            'INSERT OR REPLACE INTO subscriptions (wallet, expires_at, tx_hash) VALUES (?, ?, ?)',
            (w, expires_at, tx_hash)
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'expires_at': expires_at})
    except Exception as e:
        return jsonify({'error': f'Verification error: {str(e)[:200]}'}), 500


@app.route('/api/chat', methods=['POST'])
def api_chat():
    data    = request.json or {}
    message = (data.get('message') or '').strip()
    wallet  = (data.get('wallet') or '').lower().strip()
    context = (data.get('context') or '').strip()
    if not message:
        return jsonify({'error': 'message required'}), 400
    if not wallet:
        return jsonify({'error': 'wallet required — connect and subscribe first'}), 401
    # Subscription gate
    if wallet not in PREMIUM_WHITELIST:
        conn = get_db()
        row  = conn.execute('SELECT expires_at FROM subscriptions WHERE wallet = ?', (wallet,)).fetchone()
        conn.close()
        if not row or row['expires_at'] <= int(time.time()):
            return jsonify({'error': 'Active subscription required to use Coach.'}), 402
    if not PRIVATE_KEY:
        return jsonify({'error': 'AI not configured — contact support'}), 503
    job_id = _start_job(message, context)
    print(f'[JOB {job_id}] started — msg={message[:60]}')
    return jsonify({'job_id': job_id, 'status': 'pending'}), 202


@app.route('/api/chat/status')
def api_chat_status():
    job_id = request.args.get('job_id', '')
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'job not found'}), 404
    return jsonify({
        'status': job['status'],
        'reply':  job.get('reply'),
        'error':  job.get('error'),
    })


def _valid_wallet(w):
    w = (w or "").lower().strip()
    if len(w) != 42 or not w.startswith("0x"):
        return None
    if any(c not in "0123456789abcdef" for c in w[2:]):
        return None
    return w


MAX_STATE_BYTES = 256 * 1024   # 256 KB — a paper-trading save is a few KB; this is a generous cap


@app.route('/api/lc/save-progress', methods=['POST'])
def api_save_progress():
    data  = request.json or {}
    w     = _valid_wallet(data.get('wallet'))
    state = data.get('state')
    if not w:
        return jsonify({'error': 'valid wallet required'}), 400
    if state is None:
        return jsonify({'error': 'state required'}), 400
    # Normalize to a compact JSON string regardless of whether the client sent an object or string.
    try:
        state_str = state if isinstance(state, str) else json.dumps(state, separators=(',', ':'))
    except Exception:
        return jsonify({'error': 'state not serializable'}), 400
    if len(state_str.encode('utf-8')) > MAX_STATE_BYTES:
        return jsonify({'error': 'state too large'}), 413
    now = int(time.time())
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO progress (wallet, state, updated_at) VALUES (?, ?, ?)',
        (w, state_str, now)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'updated_at': now})


@app.route('/api/lc/load-progress/<wallet>')
def api_load_progress(wallet):
    w = _valid_wallet(wallet)
    if not w:
        return jsonify({'error': 'valid wallet required'}), 400
    conn = get_db()
    row  = conn.execute('SELECT state, updated_at FROM progress WHERE wallet = ?', (w,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'state': None, 'updated_at': None})
    try:
        parsed = json.loads(row['state'])
    except Exception:
        parsed = None
    return jsonify({'state': parsed, 'updated_at': row['updated_at']})


if __name__ == '__main__':
    if not PRIVATE_KEY:
        print('[WARN] LIGHTCHAIN_PRIVATE_KEY not set — AI endpoints will return 503')
    app.run(host='0.0.0.0', port=PORT)
