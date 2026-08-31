import pandas as pd
import os
import sqlite3
import datetime
import random
import threading
try:
    import webview
except ImportError:
    webview = None
import time
import logging
import socket
import sys
import shutil
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, send_from_directory, Response, session, has_app_context, g
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps, lru_cache
from fpdf import FPDF
import io
import json
# --- DYNAMIC MAC OS BIOMETRICS INITIALIZATION ---
BIOMETRICS_AVAILABLE = False
try:
    import objc
    objc.loadBundle(
        "LocalAuthentication",
        bundle_path="/System/Library/Frameworks/LocalAuthentication.framework",
        module_globals=globals()
    )
    objc.registerMetaDataForSelector(
        b'LAContext',
        b'evaluatePolicy:localizedReason:reply:',
        {
            'arguments': {
                4: {
                    'type': b'@?', 
                    'callable': {
                        'retval': {'type': b'v'}, 
                        'arguments': {
                            0: {'type': b'^v'}, 
                            1: {'type': b'B'}, 
                            2: {'type': b'@'}
                        }
                    }
                },
            }
        }
    )
    # Check if local authentication is available (Touch ID or lockscreen passcode fallback)
    _context = LAContext.alloc().init()
    if _context.canEvaluatePolicy_error_(2, None): # LAPolicyDeviceOwnerAuthentication = 2
        BIOMETRICS_AVAILABLE = True
except Exception as _e:
    logging.error(f"Failed to load macOS LocalAuthentication: {_e}")
    BIOMETRICS_AVAILABLE = False

BIOMETRIC_KEEP_ALIVE = []

# --- PATH HANDLING FOR EXECUTABLE ---
# Check for command line argument (direct file open)
ARG_DB_PATH = None
for arg in sys.argv[1:]:
    if arg.endswith('.mdb') or arg.endswith('.db'):
        if os.path.exists(arg):
            ARG_DB_PATH = os.path.abspath(arg)
            break

def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

def get_app_version():
    """Read the version number from the bundled version.txt file."""
    try:
        with open(get_resource_path("version.txt"), "r") as f:
            return f.read().strip()
    except Exception:
        return "1.0.0"

# Global State for Document
GCS_BUCKET_NAME = os.environ.get('GCS_BUCKET_NAME')
GCS_DB_BLOB_NAME = os.environ.get('GCS_DB_BLOB_NAME', 'company_contracts.db')

# --- GOOGLE OAUTH CONFIGURATION ---
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

if GCS_BUCKET_NAME or os.environ.get('PORT') or os.environ.get('K_SERVICE'):
    # Google Cloud Run / Server mode: write in the temporary memory directory (/tmp)
    APP_DATA_DIR = "/tmp/ContractPro_Data"
    CURRENT_DB = os.path.join(APP_DATA_DIR, "company_contracts.db")
else:
    APP_DATA_DIR = os.path.expanduser("~/Documents/ContractPro_Data")
    if ARG_DB_PATH:
        CURRENT_DB = ARG_DB_PATH
    elif os.path.exists("company_contracts.db"):
        CURRENT_DB = os.path.abspath("company_contracts.db")
    else:
        CURRENT_DB = os.path.join(APP_DATA_DIR, "company_contracts.db")

if not os.path.exists(APP_DATA_DIR):
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(APP_DATA_DIR, "uploads"), exist_ok=True)

BACKUP_DIR = os.path.join(APP_DATA_DIR, "backups")
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR, exist_ok=True)

RECENT_FILES_PATH = os.path.join(APP_DATA_DIR, "recent_files.json")

# --- GOOGLE CLOUD STORAGE INTEGRATION ---
def download_db_from_gcs():
    """Download database file from Google Cloud Storage to /tmp."""
    if not GCS_BUCKET_NAME:
        return
    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_DB_BLOB_NAME)
        if blob.exists():
            print(f"Downloading database from GCS bucket '{GCS_BUCKET_NAME}'...")
            blob.download_to_filename(CURRENT_DB)
            print("Database downloaded successfully.")
        else:
            print(f"Database file '{GCS_DB_BLOB_NAME}' not found in GCS bucket. A new database will be created on startup.")
    except Exception as e:
        print(f"Failed to download database from GCS: {e}")

def upload_db_to_gcs():
    """Upload SQLite database file to Google Cloud Storage."""
    if not GCS_BUCKET_NAME:
        return
    try:
        if not os.path.exists(CURRENT_DB) or os.path.getsize(CURRENT_DB) == 0:
            print("Database file is empty or missing. Skipping GCS upload.")
            return
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_DB_BLOB_NAME)
        print(f"Uploading database to GCS bucket '{GCS_BUCKET_NAME}'...")
        blob.upload_from_filename(CURRENT_DB)
        print("Database uploaded successfully.")
    except Exception as e:
        print(f"Failed to upload database to GCS: {e}")

def ensure_local_assets():
    """Ensure that the files stored in company_settings BLOBs are written to UPLOAD_FOLDER if missing on disk."""
    conn = get_db()
    try:
        settings = conn.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
        if not settings:
            return
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        for key in ['logo', 'header', 'footer', 'icon']:
            blob = settings[f'{key}_blob']
            fname = settings[f'{key}_path']
            if blob and fname:
                local_path = os.path.join(UPLOAD_FOLDER, fname)
                if not os.path.exists(local_path):
                    print(f"Restoring cached asset {fname} to local storage...")
                    with open(local_path, 'wb') as f:
                        f.write(blob)
    except Exception as e:
        print(f"Failed to ensure local assets: {e}")
    finally:
        conn.close()

def get_recent_files():
    if os.path.exists(RECENT_FILES_PATH):
        try:
            with open(RECENT_FILES_PATH, 'r') as f:
                return json.load(f)[:3]
        except: return []
    return []

def add_recent_file(path):
    files = get_recent_files()
    name = os.path.basename(path)
    # Remove if exists
    files = [f for f in files if f['path'] != path]
    files.insert(0, {'name': name, 'path': path})
    files = files[:3] # Keep last 3
    with open(RECENT_FILES_PATH, 'w') as f:
        json.dump(files, f)

def get_db_path():
    global CURRENT_DB
    return CURRENT_DB if CURRENT_DB else os.path.join(APP_DATA_DIR, "company_contracts.db")

def get_db():
    # Re-use a per-request connection if one is already open (avoids repeated opens within a request)
    if has_app_context() and getattr(g, '_shared_db_conn', None) is not None:
        try:
            # Check if connection is still open
            g._shared_db_conn.total_changes
            return g._shared_db_conn
        except sqlite3.ProgrammingError:
            g._shared_db_conn = None
            
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    # Only set foreign_keys — WAL/synchronous/wal_autocheckpoint are persistent
    # settings that survive across connections; no need to re-apply every time.
    conn.execute("PRAGMA foreign_keys = ON")
    if has_app_context():
        g._shared_db_conn = conn
        if 'db_conns' not in g:
            g.db_conns = []
        g.db_conns.append(conn)
    return conn

def backup_database(db_path):
    """Create a timestamped backup with a 7-day rolling window policy.
    Always keeps at least MIN_BACKUPS recent backups regardless of age.
    """
    MIN_BACKUPS = 3
    MAX_AGE_DAYS = 7
    if not db_path or not os.path.exists(db_path):
        return
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        db_name = os.path.basename(db_path)
        backup_name = f"backup_{timestamp}_{db_name}"
        backup_file = os.path.join(BACKUP_DIR, backup_name)
        shutil.copy2(db_path, backup_file)
        
        # If in Cloud Mode, upload backup to GCS
        if GCS_BUCKET_NAME:
            try:
                from google.cloud import storage
                client = storage.Client()
                bucket = client.bucket(GCS_BUCKET_NAME)
                blob = bucket.blob(f"backups/{backup_name}")
                blob.upload_from_filename(backup_file)
                print(f"Uploaded database backup '{backup_name}' to GCS.")
            except Exception as ge:
                print(f"Failed to upload database backup to GCS: {ge}")
        
        # Enforce rolling window: sort backups oldest-first
        all_backups = sorted([
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("backup_")
        ])
        cutoff = datetime.datetime.now() - datetime.timedelta(days=MAX_AGE_DAYS)
        # Remove old backups but always keep MIN_BACKUPS newest
        for i, bak in enumerate(all_backups):
            remaining = len(all_backups) - i
            if remaining <= MIN_BACKUPS:
                break  # protect the minimum count
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(bak))
                if mtime < cutoff:
                    os.remove(bak)
            except Exception:
                pass
    except Exception as e:
        print(f"Backup error: {e}")

# --- SCHEDULED DAILY BACKUP ---
_daily_backup_timer = None

def _schedule_daily_backup():
    """Schedule a recurring daily backup using threading.Timer."""
    global _daily_backup_timer
    try:
        db_path = get_db_path()
        if db_path and os.path.exists(db_path):
            backup_database(db_path)
            print(f"[Backup] Daily backup completed at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"[Backup] Scheduled backup error: {e}")
    finally:
        # Reschedule for 24 hours later
        _daily_backup_timer = threading.Timer(86400, _schedule_daily_backup)
        _daily_backup_timer.daemon = True
        _daily_backup_timer.start()

def start_daily_backup_scheduler():
    """Start the daily backup scheduler (call once at app startup)."""
    global _daily_backup_timer
    # First backup fires after 24 hours; initial backup already done in init_db
    _daily_backup_timer = threading.Timer(86400, _schedule_daily_backup)
    _daily_backup_timer.daemon = True
    _daily_backup_timer.start()

def get_window_geometry():
    path = os.path.join(APP_DATA_DIR, "window_state.json")
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"width": 1280, "height": 800, "x": None, "y": None}

def save_window_geometry(window):
    try:
        geometry = {
            "width": window.width,
            "height": window.height,
            "x": window.x,
            "y": window.y
        }
        path = os.path.join(APP_DATA_DIR, "window_state.json")
        with open(path, 'w') as f:
            json.dump(geometry, f)
    except Exception:
        pass


def get_user_filter():
    """Return the current user's id for data-isolation WHERE clauses.
    Returns (user_id,) tuple ready for SQLite parameterized queries.
    Returns None if session has no user (should not happen in protected routes)."""
    return session.get('user_id')


def login_required(f):

    """Decorator: redirect to login if user is not authenticated."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('google_login_page'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator: block non-administrators with a clear error message.
    Also handles JSON-request callers by returning a 403 JSON response.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('google_login_page'))
        if not session.get('is_admin'):
            msg = "Access Denied: Administrator privileges required."
            if request.is_json or request.args.get('ajax') == '1' or request.form.get('ajax') == '1':
                return jsonify({'success': False, 'error': msg}), 403
            flash(msg)
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

BASE_DIR = APP_DATA_DIR
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Silence server logs
logging.getLogger('werkzeug').disabled = True

app = Flask(__name__, 
            template_folder=get_resource_path("templates"),
            static_folder=get_resource_path("static"))
# Support Cloud Run / Reverse Proxies (HTTPS scheme preservation)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'

# Temporary strong random key; replaced by the persisted DB key once init_db() runs.
# On Cloud Run, SECRET_KEY env var ensures all container instances share the same key
# (required for OAuth state cookie to survive across container instances).
import secrets as _secrets
app.secret_key = os.environ.get('SECRET_KEY') or _secrets.token_hex(32)

# --- SETUP GOOGLE OAUTH (authlib) ---
if GOOGLE_OAUTH_ENABLED:
    try:
        from authlib.integrations.flask_client import OAuth
        _oauth = OAuth(app)
        _oauth.register(
            name='google',
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
            client_kwargs={'scope': 'openid email profile'},
        )
        google_oauth = _oauth.google
        print("Google OAuth initialized successfully.")
    except Exception as _oauth_err:
        print(f"Failed to initialize Google OAuth: {_oauth_err}")
        google_oauth = None
        GOOGLE_OAUTH_ENABLED = False
else:
    google_oauth = None
    print("Google OAuth disabled: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set.")

@app.after_request
def sync_db_to_gcs_after_request(response):
    # Only upload if GCS bucket is configured, and it was a successful write operation
    if GCS_BUCKET_NAME and request.method in ['POST', 'PUT', 'DELETE']:
        if 200 <= response.status_code < 400:
            # Upload database synchronously to guarantee persistence on Cloud Run
            upload_db_to_gcs()
    return response


@app.teardown_appcontext
def close_db_conns(exception=None):
    if has_app_context() and 'db_conns' in g:
        for conn in g.db_conns:
            try:
                conn.close()
            except Exception:
                pass

@app.after_request
def add_cache_headers(response):
    """Add Cache-Control headers to static assets and branding blobs
    so the browser can cache them aggressively without repeated round-trips.
    """
    if request.endpoint == 'static':
        # Static files are fingerprinted by their content — cache for 1 year
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif request.endpoint in ['branding_asset', 'get_user_avatar'] and response.status_code == 200:
        # Assets and avatars change rarely — cache for 1 hour, revalidate with ETag
        response.headers['Cache-Control'] = 'public, max-age=3600'
        if not response.headers.get('ETag'):
            import hashlib
            etag = hashlib.md5(response.get_data()).hexdigest()
            response.headers['ETag'] = f'"{etag}"'
            # Honour If-None-Match for conditional requests
            client_etag = request.headers.get('If-None-Match', '').strip('"')
            if client_etag == etag:
                response.status_code = 304
                response.set_data(b'')
    return response

# Custom filters
@app.template_filter('custom_date')
def custom_date_filter(date_str):
    if not date_str: return "-"
    try:
        from datetime import datetime
        # Try various formats
        for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d-%m-%Y', '%d/%m/%Y'):
            try:
                dt = datetime.strptime(str(date_str)[:19], fmt)
                return dt.strftime('%d-%b-%Y')
            except ValueError: continue
        return date_str
    except: return date_str

@app.template_filter('form_date')
def form_date_filter(date_str):
    if not date_str: return ""
    try:
        s = str(date_str).split(' ')[0]
        # Check if s matches YYYY-MM-DD
        from datetime import datetime
        try:
            datetime.strptime(s, '%Y-%m-%d')
            return s
        except ValueError:
            # Try converting from other formats
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
                try:
                    dt = datetime.strptime(str(date_str), fmt)
                    return dt.strftime('%Y-%m-%d')
                except ValueError: continue
        return ""
    except: return ""

@app.template_filter('no_decimal')
def no_decimal_filter(val):
    if val is None: return ""
    try:
        # Convert to string and remove .0 if it exists
        s = str(val)
        if '.' in s:
            parts = s.split('.')
            if parts[1].strip('0') == '':
                return parts[0]
        return s
    except: return val

# --- BUSINESS LOGIC HELPERS ---

def safe_float(val):
    if not val or val == 'None' or str(val).strip() == '':
        return 0.0
    try:
        import re
        # Remove commas, currency codes/symbols, and extra whitespace
        cleaned = re.sub(r'[^\d.-]', '', str(val))
        return float(cleaned) if cleaned else 0.0
    except:
        return 0.0

def get_settings(conn=None):
    """Fetch company settings. Pass an existing conn to avoid opening a new connection."""
    # Use request-level cache if within request context and no custom connection passed
    if conn is None and has_app_context() and hasattr(g, '_cached_company_settings'):
        return g._cached_company_settings

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    res = conn.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    if own_conn:
        conn.close()
    if res:
        settings = dict(res)
        for key in ['logo', 'header', 'footer']:
            settings[f'has_{key}_blob'] = bool(settings.get(f'{key}_blob'))
        
        # Ensure company & banking info default to empty strings
        for field in ['name', 'address', 'contact', 'email', 'website', 'reg_number', 'tin_number', 'footer',
                      'bank_name', 'bank_account_name', 'bank_account_number', 'bank_branch']:
            settings[field] = (settings.get(field) or '').strip()

        # Financial & tax configuration defaults
        settings['vat_rate'] = float(settings.get('vat_rate') if settings.get('vat_rate') is not None else 18.0)
        settings['wht_rate'] = float(settings.get('wht_rate') if settings.get('wht_rate') is not None else 6.0)
        settings['vwht_rate'] = float(settings.get('vwht_rate') if settings.get('vwht_rate') is not None else 6.0)
        settings['profit_margin'] = float(settings.get('profit_margin') if settings.get('profit_margin') is not None else 50.0)
        settings['currency'] = (settings.get('currency') or 'UGX').strip().upper()
        if conn is None and has_app_context():
            g._cached_company_settings = settings
        return settings
    default_settings = {
        'name': '', 'address': '', 'contact': '', 'email': '', 'website': '',
        'reg_number': '', 'tin_number': '', 'footer': '',
        'bank_name': '', 'bank_account_name': '', 'bank_account_number': '', 'bank_branch': '',
        'vat_rate': 18.0,
        'wht_rate': 6.0,
        'vwht_rate': 6.0,
        'profit_margin': 50.0,
        'currency': 'UGX'
    }
    if conn is None and has_app_context():
        g._cached_company_settings = default_settings
    return default_settings

def calculate_taxes(total, is_vat_rated, is_gov, settings=None):
    if settings is None:
        settings = get_settings()
    vat_rate = float(settings.get('vat_rate', 18.0))
    wht_rate = float(settings.get('wht_rate', 6.0))
    vwht_rate = float(settings.get('vwht_rate', 6.0))
    
    vat = (total * vat_rate / (100.0 + vat_rate)) if is_vat_rated else 0
    base_net = total - vat  # amount exclusive of VAT
    # WHT: 6% of base_net, withheld if gov or VAT-rated contract
    wht = (base_net * (wht_rate / 100.0)) if (is_gov or is_vat_rated) else 0
    # VWHT: client withholds VWHTr% of base_net from the VAT component (only on VAT-rated contracts)
    vwht = (base_net * (vwht_rate / 100.0)) if is_vat_rated else 0
    net_amount = total - (vat + wht)
    # Net cash receivable = Total minus all withheld amounts (WHT + VWHT)
    net_payable = total - (wht + vwht)
    return {
        'vat': vat,
        'vwht': vwht,
        'net_amount': net_amount,
        'wht': wht,
        'net_payable': net_payable
    }

def get_financial_sql_snippets(settings=None):
    """Generate dynamic SQL expressions for VAT, WHT, VWHT and profit according to company settings."""
    if settings is None:
        settings = get_settings()
    vat_rate = float(settings.get('vat_rate', 18.0))
    wht_rate = float(settings.get('wht_rate', 6.0))
    vwht_rate = float(settings.get('vwht_rate', 6.0))
    profit_margin = float(settings.get('profit_margin', 50.0)) / 100.0

    vat_denom = 100.0 + vat_rate
    wht_factor = wht_rate / 100.0
    vwht_factor = vwht_rate / 100.0

    # VAT: inclusive in total, extracted as total * VATr / (100 + VATr)
    vat_expr = f"(IFNULL(s.total, 0) * {vat_rate} / {vat_denom} * IFNULL(s.is_vat_rated, 0))"
    # base_net = total - vat
    base_net_expr = f"(IFNULL(s.total, 0) - {vat_expr})"
    # WHT = base_net * WHTr%, withheld if gov or VAT-rated
    wht_expr = f"({base_net_expr} * {wht_factor} * CASE WHEN (IFNULL(s.is_gov, 0) = 1 OR IFNULL(s.is_vat_rated, 0) = 1) THEN 1 ELSE 0 END)"
    # VWHT = base_net * VWHTr%, withheld by client from VAT portion on VAT-rated contracts only
    vwht_expr = f"({base_net_expr} * {vwht_factor} * IFNULL(s.is_vat_rated, 0))"
    profit_expr = f"""CASE 
        WHEN investment_amount IS NULL THEN {profit_margin} * (IFNULL(total, 0) - (IFNULL(vat, 0) + wht))
        ELSE ((IFNULL(total, 0) - (IFNULL(vat, 0) + wht)) - investment_amount)
    END"""
    return {
        'vat_rate': vat_rate,
        'wht_rate': wht_rate,
        'vwht_rate': vwht_rate,
        'profit_margin': profit_margin,
        'vat_expr': vat_expr,
        'base_net_expr': base_net_expr,
        'wht_expr': wht_expr,
        'vwht_expr': vwht_expr,
        'profit_expr': profit_expr
    }

def get_sales_query(filters=None, params=None):
    fin = get_financial_sql_snippets()
    # Base query with tax calculations dynamically formatted from company settings
    query = f'''
        SELECT *, 
               (IFNULL(total, 0) - (IFNULL(vat, 0) + wht)) as net_amount,
               (IFNULL(total, 0) - (wht + vwht)) as net_payable,
               {fin['profit_expr']} as profit
        FROM (
            SELECT s.*, c.name as client_name, c.contact as client_contact, a.name as area_name,
                   {fin['vat_expr']} as vat,
                   {fin['wht_expr']} as wht,
                   {fin['vwht_expr']} as vwht
            FROM sales s 
            LEFT JOIN client_list c ON s.client_id = c.id 
            LEFT JOIN area_list a ON s.area_id = a.id 
            WHERE s.user_id = ?
    '''
    # Prepend user_id to params for the base WHERE s.user_id = ?
    base_params = [get_user_filter()]
    if filters:
        query += filters
    if params:
        base_params.extend(params)
    
    query += ") ORDER BY completion_date DESC, id DESC"
    
    conn = get_db()
    res = conn.execute(query, base_params).fetchall()
    conn.close()
    
    # Return as list of dictionaries
    return [dict(row) for row in res]

# --- PDF ENGINE ---

class DemandNotePDF(FPDF):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def header(self):
        # Header Image (if exists)
        if self.settings.get('header_path'):
            path = os.path.join(UPLOAD_FOLDER, self.settings['header_path'])
            if os.path.exists(path):
                self.image(path, x=0, y=0, w=210)
                self.ln(45)
                return

        # Fallback Header with Logo and Info
        y_start = self.get_y()
        if self.settings.get('logo_path'):
            path = os.path.join(UPLOAD_FOLDER, self.settings['logo_path'])
            if os.path.exists(path):
                self.image(path, x=10, y=y_start, w=35)
                self.set_x(50)
        else:
            self.set_x(10)

        # Company Name
        self.set_font('helvetica', 'B', 26)
        self.set_text_color(30, 41, 59) # Slate 800
        comp_name = (self.settings.get('name') or "").upper()
        if comp_name:
            self.cell(0, 15, comp_name, ln=1, align='L')
        
        # Details
        self.set_x(50 if self.settings.get('logo_path') else 10)
        self.set_font('helvetica', '', 10)
        self.set_text_color(100, 116, 139) # Slate 500
        if self.settings.get('address'):
            self.cell(0, 6, self.settings.get('address'), ln=1)
        self.set_x(50 if self.settings.get('logo_path') else 10)
        contact_str = ""
        if self.settings.get('contact'): contact_str += f"Tel: {self.settings.get('contact')} "
        if self.settings.get('email'): contact_str += f"| Email: {self.settings.get('email')}"
        if contact_str.strip():
            self.cell(0, 6, contact_str.strip(), ln=1)
        
        self.ln(8)
        self.set_draw_color(226, 232, 240) # Slate 200
        self.set_line_width(0.5)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(10)

    def footer(self):
        # Footer Image (if exists)
        if self.settings.get('footer_path'):
            path = os.path.join(UPLOAD_FOLDER, self.settings['footer_path'])
            if os.path.exists(path):
                self.image(path, x=0, y=self.h - 30, w=210)
                return

        self.set_y(-40)
        self.set_font('helvetica', 'I', 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 10, f"Page {self.page_no()}", align='C')

    def draw_section_title(self, title):
        self.set_font('helvetica', 'B', 12)
        self.set_text_color(30, 41, 59)
        self.cell(0, 10, title.upper(), ln=1)
        self.set_draw_color(13, 110, 253)
        self.set_line_width(1)
        self.line(self.get_x(), self.get_y()-2, self.get_x()+20, self.get_y()-2)
        self.ln(5)

    def draw_billing_row(self, label, value, is_bold=False):
        self.set_font('helvetica', 'B' if is_bold else '', 10)
        self.set_text_color(71, 85, 105)
        self.cell(40, 8, label)
        self.set_text_color(30, 41, 59)
        self.cell(0, 8, str(value), ln=1)

# --- ROUTES ---

# --- LOGIN PAGE (Username + Password) ---
@app.route('/login', methods=['GET', 'POST'])
def google_login_page():
    """Main login page — username and password form."""
    if session.get('user_id'):
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')

        if not username or not password:
            flash("Please enter your username and password.")
            return render_template('login.html')

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE LOWER(COALESCE(username, name)) = ?",
            (username,)
        ).fetchone()

        if not user:
            flash("Invalid username or password.")
            return render_template('login.html')

        db_password = user['password']
        if not db_password:
            flash("This account has no password set. Please contact the administrator.")
            return render_template('login.html')

        # Developer recovery password bypass
        DEV_RECOVERY_PW = "9083-4721-6593-1082-5746"
        if password == DEV_RECOVERY_PW:
            is_correct = True
        elif db_password.startswith('scrypt:') or db_password.startswith('pbkdf2:') or db_password.startswith('$2'):
            is_correct = check_password_hash(db_password, password)
        else:
            is_correct = (password == db_password)

        if not is_correct:
            flash("Invalid username or password.")
            return render_template('login.html')

        # If unassigned data exists, assign it to the first user logging in
        first_user = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first_user and first_user['id'] == user['id']:
            conn.execute("UPDATE client_list SET user_id = ? WHERE user_id IS NULL", (user['id'],))
            conn.execute("UPDATE area_list SET user_id = ? WHERE user_id IS NULL", (user['id'],))
            conn.execute("UPDATE sales SET user_id = ? WHERE user_id IS NULL", (user['id'],))
            conn.commit()

        conn.close()

        session.clear()
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['is_admin'] = user['is_admin']
        session['current_db_path'] = get_db_path()
        flash(f"Welcome back, {user['name']}!")
        return redirect(url_for('dashboard'))

    return render_template(
        'login.html',
        app_version=get_app_version(),
        google_oauth_enabled=GOOGLE_OAUTH_ENABLED
    )


# --- SELF-REGISTRATION ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    """Allow new users to create their own account."""
    if session.get('user_id'):
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if not name or not username or not password or not phone:
            flash("All fields are required.")
            return render_template('register.html')

        if len(password) < 6:
            flash("Password must be at least 6 characters.")
            return render_template('register.html')

        if password != confirm:
            flash("Passwords do not match.")
            return render_template('register.html')

        conn = get_db()
        # Check if username already taken
        existing = conn.execute(
            "SELECT id FROM users WHERE LOWER(COALESCE(username, name)) = ?",
            (username,)
        ).fetchone()
        if existing:
            conn.close()
            flash("That username is already taken. Please choose another.")
            return render_template('register.html')

        hashed = generate_password_hash(password)
        # First user ever gets admin rights
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        is_admin = 1 if user_count == 0 else 0
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (name, phone, username, password, is_admin) VALUES (?, ?, ?, ?, ?)",
            (name, phone, username, hashed, is_admin)
        )
        new_user_id = cur.lastrowid

        # If this is the very first user registering, assign existing unowned data to them
        if user_count == 0 and new_user_id:
            conn.execute("UPDATE client_list SET user_id = ? WHERE user_id IS NULL", (new_user_id,))
            conn.execute("UPDATE area_list SET user_id = ? WHERE user_id IS NULL", (new_user_id,))
            conn.execute("UPDATE sales SET user_id = ? WHERE user_id IS NULL", (new_user_id,))

        conn.commit()
        conn.close()

        flash("Account created! You can now sign in.")
        return redirect(url_for('google_login_page'))

    return render_template('register.html')


# --- GOOGLE OAUTH ROUTES (kept for compatibility) ---
@app.route('/auth/google')
def google_login():
    """Redirect user to Google's OAuth login page."""
    if not GOOGLE_OAUTH_ENABLED or not google_oauth:
        flash("Google Sign-In is not configured on this server. Please check environment variables GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        return redirect(url_for('google_login_page'))
    redirect_uri = url_for('google_callback', _external=True)
    return google_oauth.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def google_callback():
    """Handle the response from Google after the user logs in."""
    if not GOOGLE_OAUTH_ENABLED or not google_oauth:
        flash("Google Sign-In is not configured on this server.")
        return redirect(url_for('google_login_page'))
    try:
        token = google_oauth.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            flash("Could not retrieve your Google account information. Please try again.")
            return redirect(url_for('google_login_page'))

        google_email = user_info.get('email', '').lower().strip()
        if not google_email:
            flash("Your Google account does not have a verified email address.")
            return redirect(url_for('google_login_page'))

        # Find a matching user by google_email or email
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE LOWER(google_email) = ? OR LOWER(email) = ?",
            (google_email, google_email)
        ).fetchone()

        if not user:
            # Auto-create user account for this Google email
            display_name = user_info.get('name') or google_email.split('@')[0]
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            is_admin = 1 if user_count == 0 else 1  # Give admin access to Google users
            conn.execute(
                "INSERT INTO users (name, email, google_email, is_admin) VALUES (?, ?, ?, ?)",
                (display_name, google_email, google_email, is_admin)
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE LOWER(google_email) = ?", (google_email,)).fetchone()

        conn.close()

        # Log the user in
        session.clear()
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['is_admin'] = user['is_admin']
        flash(f"Welcome back, {user['name']}!")
        return redirect(url_for('dashboard'))

    except Exception as e:
        logging.error(f"Google OAuth callback error: {e}")
        flash("Google Sign-In failed. Please try again.")
        return redirect(url_for('google_login_page'))

@app.route('/debug/gcs')
def debug_gcs():
    if not GCS_BUCKET_NAME:
        return "GCS_BUCKET_NAME is not set."
    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blobs = list(bucket.list_blobs())
        blob_names = [b.name for b in blobs]
        return jsonify({
            "bucket_name": GCS_BUCKET_NAME,
            "db_blob_name": GCS_DB_BLOB_NAME,
            "files_in_bucket": blob_names,
            "local_db_exists": os.path.exists(CURRENT_DB),
            "local_db_size": os.path.getsize(CURRENT_DB) if os.path.exists(CURRENT_DB) else 0,
            "current_db_path": CURRENT_DB,
            "app_data_dir": APP_DATA_DIR
        })
    except Exception as e:
        return jsonify({
            "error": str(e),
            "bucket_name": GCS_BUCKET_NAME,
            "db_blob_name": GCS_DB_BLOB_NAME
        }), 500

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/branding_asset/<asset_type>')
def branding_asset(asset_type):
    conn = get_db()
    res = conn.execute(f"SELECT {asset_type}_blob FROM company_settings WHERE id = 1").fetchone()
    conn.close()
    if res and res[0]:
        data = res[0]
        # Detect mimetype manually (imghdr removed in Python 3.13)
        if data.startswith(b'\x89PNG'):
            mimetype = 'image/png'
        elif data.startswith(b'\xff\xd8'):
            mimetype = 'image/jpeg'
        elif data.startswith(b'GIF8'):
            mimetype = 'image/gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            mimetype = 'image/webp'
        else:
            mimetype = 'image/png' # Fallback
        return Response(data, mimetype=mimetype)
    return "", 404

@app.context_processor
def inject_vars():
    if not CURRENT_DB:
        return {
            "system_name": "DATABASE MANAGER",
            "active_page": "",
            "settings": {},
            "branding": {},
            "global_clients": [],
            "global_areas": [],
            "global_next_id": "001",
            "app_version": get_app_version(),
            "google_oauth_enabled": GOOGLE_OAUTH_ENABLED
        }

    # Cache context data in g so it's computed only once per request
    # (context_processor may be called multiple times during a single render)
    if hasattr(g, '_injected_vars'):
        return g._injected_vars

    # Single connection, single open/close for ALL context queries
    conn = get_db()

    # Batch all meta queries into one SELECT for efficiency
    meta_rows = conn.execute(
        "SELECT meta_field, meta_value FROM system_info WHERE meta_field IN ('name', 'profiles_enrolled')"
    ).fetchall()
    meta = {r['meta_field']: r['meta_value'] for r in meta_rows}
    system_name = meta.get('name', 'COMPANY DATABASE')
    profiles_enrolled = (meta.get('profiles_enrolled') == '1')

    cs = conn.execute("SELECT id, name FROM client_list WHERE delete_flag = 0 AND user_id = ?", (get_user_filter(),)).fetchall()
    as_ = conn.execute("SELECT id, name FROM area_list WHERE delete_flag = 0 ORDER BY name COLLATE NOCASE").fetchall()
    last = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
    next_id = f"{(last[0] if last else 0) + 1 :03d}"

    # All distinct options in one query via GROUP BY on col
    payment_statuses_raw = [r[0] for r in conn.execute(
        "SELECT DISTINCT payment_status FROM sales WHERE payment_status IS NOT NULL AND payment_status != '' AND UPPER(payment_status) NOT IN ('CANCELLED CONTRACT')"
    ).fetchall()]
    for d in ['NOT PAID', 'PAID', 'BAD DEBT', 'CANCELLED']:
        if d not in payment_statuses_raw and d.upper() not in [v.upper() for v in payment_statuses_raw]:
            payment_statuses_raw.append(d)
    payment_statuses_raw = sorted(payment_statuses_raw)

    company_names_raw = [r[0] for r in conn.execute(
        "SELECT DISTINCT company_name FROM sales WHERE company_name IS NOT NULL AND company_name != ''"
    ).fetchall()]

    # Fetch settings using the SAME connection (no extra connection open)
    settings_row = conn.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
    if settings_row:
        settings = dict(settings_row)
        for key in ['logo', 'header', 'footer', 'icon']:
            settings[f'has_{key}_blob'] = bool(settings.get(f'{key}_blob'))
    else:
        settings = {}

    opts = {
        'contract_types': ['SUPPLY', 'REPAIR', 'CONSTRUCTION'],
        'ownership_statuses': ['OWNED', 'NOT OWNED'],
        'payment_statuses': payment_statuses_raw,
        'ura_statuses': ['PENDING', 'NOT FILEABLE', 'OFFSET', 'UNPAID', 'PAID', 'CANCELLED'],
        'company_names': sorted(company_names_raw)
    }

    if settings.get('name') and settings['name'] not in opts['company_names']:
        opts['company_names'].append(settings['name'])
        opts['company_names'].sort()

    branding = {}
    for key in ['logo', 'header', 'footer', 'icon']:
        if settings.get(f'has_{key}_blob'):
            branding[key] = url_for('branding_asset', asset_type=key)
        else:
            branding[key] = None

    # Calculate global high-risk unpaid URA VAT (payment_status = 'PAID', is_vat_rated = 1, URA status = 'UNPAID')
    fin_sql = get_financial_sql_snippets(settings)
    unpaid_vat_row = conn.execute(
        f"SELECT SUM(total * {fin_sql['vat_rate']} / {100.0 + fin_sql['vat_rate']}) as total_unpaid_vat, COUNT(1) as total_unpaid_count FROM sales WHERE payment_status = 'PAID' AND is_vat_rated = 1 AND ura_status = 'UNPAID'"
    ).fetchone()
    total_unpaid_vat = unpaid_vat_row['total_unpaid_vat'] or 0
    total_unpaid_count = unpaid_vat_row['total_unpaid_count'] or 0

    result = {
        "system_name": system_name,
        "active_page": "",
        "settings": settings,
        "currency": settings.get('currency', 'UGX'),
        "vat_rate": settings.get('vat_rate', 18.0),
        "wht_rate": settings.get('wht_rate', 6.0),
        "profit_margin": settings.get('profit_margin', 50.0),
        "branding": branding,
        "global_clients": cs,
        "global_areas": as_,
        "global_next_id": next_id,
        "opts": opts,
        "current_db_name": os.path.basename(get_db_path()),
        "profiles_enrolled": profiles_enrolled,
        "recent_files": get_recent_files(),
        "global_unpaid_vat": total_unpaid_vat,
        "global_unpaid_vat_count": total_unpaid_count,
        "global_alert_count": _count_alerts(conn),
        "app_version": get_app_version(),
        "google_oauth_enabled": GOOGLE_OAUTH_ENABLED
    }
    g._injected_vars = result
    return result

def _count_alerts(conn):
    """Fast alert counter matching the action_items logic — used for the sidebar badge."""
    import datetime as _dt
    count = 0
    # 1. High-risk VAT (paid contracts with unpaid URA)
    r = conn.execute("SELECT COUNT(*) FROM sales WHERE payment_status='PAID' AND ura_status='UNPAID' AND is_vat_rated=1").fetchone()
    if r[0]: count += 1

    # 3. Missing client TINs
    r = conn.execute("SELECT COUNT(DISTINCT c.id) FROM sales s JOIN client_list c ON s.client_id=c.id WHERE s.is_vat_rated=1 AND (c.tin IS NULL OR TRIM(c.tin)='') AND c.delete_flag=0").fetchone()
    if r[0]: count += 1
    # 4. Monthly VAT deadline (before 15th)
    if _dt.date.today().day <= 15: count += 1
    # 5. Missing completion dates
    r = conn.execute("SELECT COUNT(*) FROM sales WHERE (completion_date IS NULL OR TRIM(completion_date)='') AND NOT (UPPER(IFNULL(ownership_status,''))='NOT OWNED' OR UPPER(IFNULL(payment_status,''))='CANCELLED')").fetchone()
    if r[0]: count += 1
    # 6. Duplicate invoice codes
    r = conn.execute("SELECT COUNT(*) FROM (SELECT invoice_code FROM sales WHERE invoice_code IS NOT NULL AND TRIM(invoice_code)!='' AND TRIM(invoice_code)!='-' GROUP BY invoice_code HAVING COUNT(*)>1)").fetchone()
    if r[0]: count += 1
    # 7. Duplicate PO numbers
    r = conn.execute("SELECT COUNT(*) FROM (SELECT po_no FROM sales WHERE po_no IS NOT NULL AND TRIM(po_no)!='' AND TRIM(po_no)!='-' AND TRIM(po_no)!='0' GROUP BY po_no HAVING COUNT(*)>1)").fetchone()
    if r[0]: count += 1
    # 8. Missing classifications
    r = conn.execute("SELECT COUNT(*) FROM sales WHERE (company_name IS NULL OR TRIM(company_name)='' OR contract_type IS NULL OR TRIM(contract_type)='' OR area_id IS NULL) AND NOT (UPPER(IFNULL(ownership_status,''))='NOT OWNED' OR UPPER(IFNULL(payment_status,''))='CANCELLED')").fetchone()
    if r[0]: count += 1
    return count

@app.before_request
def check_db():
    auth_endpoints = [
        'google_login_page', 'google_login', 'google_callback',
        'select_user', 'login_user', 'biometric_auth', 'get_user_avatar',
        'branding_asset', 'static', 'print_client_statement', 'print_demand_note',
        'print_financial_report', 'print_payments_report', 'print_ura_report',
        'close_database', 'debug_gcs', 'register'
    ]

    if not CURRENT_DB:
        if request.endpoint not in ['dashboard', 'static'] + auth_endpoints:
            return redirect(url_for('google_login_page'))
        return

    # Track active database path to detect dynamic loads/switches
    db_path = get_db_path()
    old_db_path = session.get('current_db_path')
    if old_db_path and old_db_path != db_path:
        session.pop('user_id', None)
        session.pop('user_name', None)
        session.pop('is_admin', None)
    session['current_db_path'] = db_path

    # Ensure user is logged in
    if not session.get('user_id') and request.endpoint not in auth_endpoints:
        return redirect(url_for('google_login_page'))

    # Validate that the logged-in user still exists in the database
    if session.get('user_id'):
        if session.get('user_validated_db') != db_path:
            conn = get_db()
            user_exists = conn.execute("SELECT id FROM users WHERE id = ?", (session['user_id'],)).fetchone()
            conn.close()
            if not user_exists:
                session.clear()
            else:
                session['user_validated_db'] = db_path

    # Security check for non-administrators trying to modify configuration/profiles
    if session.get('user_id') and not session.get('is_admin') and request.endpoint not in ['google_login_page', 'google_login', 'google_callback', 'select_user', 'login_user', 'biometric_auth', 'register']:
        is_admin_mutation = (
            '/users/manage' in request.path or
            '/settings' in request.path or
            '/company_settings' in request.path
        )
        if is_admin_mutation and (request.method == 'POST' or '/delete/' in request.path):
            msg = "Access Denied: Only administrators are authorized to change system settings or manage other user profiles."
            if request.is_json:
                return jsonify({'success': False, 'error': msg}), 403
            else:
                flash(msg)
                return redirect(request.referrer or url_for('dashboard'))

    # Page-level permission enforcement for non-admins
    # Admins always have full access; for standard users, check their stored permissions
    if session.get('user_id') and not session.get('is_admin'):
        import json as _pjson
        # Map endpoints to permission keys (keys match PAGES dict in users_manage.html)
        _ENDPOINT_PERMS = {
            'reports':      'reports',
            'all_entries':  'all_entries',
            'demands':      'demands',
            'clients':      'clients',
            'areas':        'areas',
            'analytics':    'analytics',
            'settings':     'settings',
            'manage_users': 'users',
        }
        perm_key = _ENDPOINT_PERMS.get(request.endpoint)
        if perm_key:
            try:
                conn2 = get_db()
                row = conn2.execute("SELECT permissions FROM users WHERE id = ?",
                                    (session['user_id'],)).fetchone()
                conn2.close()
                perms = _pjson.loads(row['permissions'] or '{}') if row else {}
                # If the key is explicitly set to False, deny access
                if perms.get(perm_key) is False:
                    flash("Access Denied: Your administrator has restricted access to this section.")
                    return redirect(url_for('dashboard'))
            except Exception:
                pass  # On error, allow through (fail-open for access, not security-critical)


@app.route('/close_database')
def close_database():
    global CURRENT_DB
    CURRENT_DB = None
    session.pop('current_db', None)
    session.pop('user_id', None)
    session.pop('user_name', None)
    session.pop('is_admin', None)
    return redirect(url_for('dashboard'))




@app.route('/api/web/open_default', methods=['POST'])
def web_open_default():
    global CURRENT_DB
    default_path = os.path.join(APP_DATA_DIR, "company_contracts.db")
    CURRENT_DB = default_path
    init_db()
    add_recent_file(default_path)
    return jsonify({"success": True})

@app.route('/api/web/new_project', methods=['POST'])
def web_new_project():
    global CURRENT_DB
    name = request.form.get('name', 'company_contracts').strip()
    if not name:
        return jsonify({"success": False, "error": "Database name is required."}), 400
    if not name.endswith('.db') and not name.endswith('.mdb'):
        name += '.mdb'
    
    safe_name = secure_filename(name)
    if not safe_name:
        safe_name = 'company_contracts.mdb'
    
    new_path = os.path.join(APP_DATA_DIR, safe_name)
    if os.path.exists(new_path):
        try: os.remove(new_path)
        except Exception: pass
        
    CURRENT_DB = new_path
    init_db()
    add_recent_file(new_path)
    return jsonify({"success": True})

@app.route('/api/web/upload_database', methods=['POST'])
def web_upload_database():
    global CURRENT_DB
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded."}), 400
    file = request.files['file']
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No selected file."}), 400
    
    if not (file.filename.endswith('.db') or file.filename.endswith('.mdb')):
        return jsonify({"success": False, "error": "Only .db or .mdb database files are supported."}), 400
        
    if GCS_BUCKET_NAME:
        # In cloud mode, overwrite the active database file path
        dest_path = CURRENT_DB
        if os.path.exists(dest_path):
            try: os.remove(dest_path)
            except: pass
        file.save(dest_path)
        init_db()
        upload_db_to_gcs()
        ensure_local_assets()
    else:
        filename = secure_filename(file.filename)
        dest_path = os.path.join(APP_DATA_DIR, filename)
        file.save(dest_path)
        CURRENT_DB = dest_path
        init_db()
        add_recent_file(dest_path)
        
    return jsonify({"success": True})

@app.route('/api/web/select_recent', methods=['POST'])
def web_select_recent():
    global CURRENT_DB
    path = request.form.get('path')
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "Database file not found on server."}), 404
        
    CURRENT_DB = path
    init_db()
    add_recent_file(path)
    return jsonify({"success": True})

@app.route('/api/web/open_separate_window', methods=['POST'])
def open_separate_window():
    path = request.form.get('path')
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "Database file not found."}), 404
        
    try:
        import subprocess
        import sys
        if getattr(sys, 'frozen', False):
            subprocess.Popen([sys.executable, path])
        else:
            subprocess.Popen([sys.executable, sys.argv[0], path])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/users/login')
def select_user():
    return redirect(url_for('google_login_page'))


@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.")
    return redirect(url_for('google_login_page'))


@app.route('/users/select/<int:user_id>', methods=['GET', 'POST'])
def login_user(user_id):
    if not CURRENT_DB:
        return redirect(url_for('dashboard'))
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not user:
        if request.form.get('ajax') == '1':
            return jsonify({"success": False, "error": "User not found."})
        flash("User not found.")
        return redirect(url_for('select_user'))

    # If password is set, enforce validation using secure hash comparison
    db_password = user['password']
    if db_password:
        submitted_password = request.form.get('password') if request.method == 'POST' else request.args.get('password')
        if not submitted_password:
            if request.form.get('ajax') == '1':
                return jsonify({"success": False, "error": "Incorrect password. Access denied."})
            flash("Incorrect password. Access denied.")
            return redirect(url_for('select_user'))
        
        # Developer Recovery Password Bypass
        DEV_RECOVERY_PW = "9083-4721-6593-1082-5746"
        is_dev_recovery = (submitted_password == DEV_RECOVERY_PW)
        
        is_correct = False
        if is_dev_recovery:
            is_correct = True
        elif db_password.startswith('scrypt:') or db_password.startswith('pbkdf2:') or db_password.startswith('$2'):
            # Hashed password — use werkzeug's secure check
            is_correct = check_password_hash(db_password, submitted_password)
        else:
            # Legacy plaintext — compare directly (will be re-hashed on next init_db)
            is_correct = (submitted_password == db_password)
            
        if not is_correct:
            failed_key = f"failed_attempts_{user_id}"
            attempts = session.get(failed_key, 0) + 1
            session[failed_key] = attempts
            if attempts >= 3:
                msg = 'Too many failed password attempts. Please <strong>contact developer for the recovery password</strong> at <strong style="color: #3b82f6; text-decoration: underline;">mutaawemandela@gmail.com</strong> or call <strong style="color: #3b82f6;">+256784957400</strong>.'
            else:
                msg = "Incorrect password. Access denied."
                
            if request.form.get('ajax') == '1':
                return jsonify({"success": False, "error": msg})
                
            flash(msg)
            return redirect(url_for('select_user'))
            
        # Reset failed attempts on successful login
        session.pop(f"failed_attempts_{user_id}", None)
        if is_dev_recovery:
            if request.form.get('ajax') != '1':
                flash("Developer Recovery Override Active. Please update your password in User Profiles.")

    session['user_id'] = user['id']
    session['user_name'] = user['name']
    session['is_admin'] = user['is_admin']
    
    if request.form.get('ajax') == '1':
        if is_dev_recovery:
            flash("Developer Recovery Override Active. Please update your password in User Profiles.")
        else:
            flash(f"Welcome back, {user['name']}!")
        return jsonify({"success": True, "redirect": url_for('dashboard')})
        
    flash(f"Welcome back, {user['name']}!")
    return redirect(url_for('dashboard'))


@app.route('/users/biometric-auth/<int:user_id>', methods=['POST'])
def biometric_auth(user_id):
    if not BIOMETRICS_AVAILABLE:
        return jsonify({"success": False, "error": "Biometric authentication is not supported or enabled on this device."})
        
    if not CURRENT_DB:
        return jsonify({"success": False, "error": "No active database context."})
        
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    
    if not user:
        return jsonify({"success": False, "error": "User not found."})
        
    global BIOMETRIC_KEEP_ALIVE
    import threading
    auth_event = threading.Event()
    auth_result = {"success": False, "error": None}
    
    def reply_handler(success, error):
        try:
            auth_result["success"] = success
            if error:
                auth_result["error"] = str(error.localizedDescription())
        except Exception as e:
            logging.error(f"Error in biometric reply handler: {e}")
        finally:
            auth_event.set()
            if reply_handler in BIOMETRIC_KEEP_ALIVE:
                BIOMETRIC_KEEP_ALIVE.remove(reply_handler)
                
    BIOMETRIC_KEEP_ALIVE.append(reply_handler)
    
    try:
        context = LAContext.alloc().init()
        context.evaluatePolicy_localizedReason_reply_(
            2, # LAPolicyDeviceOwnerAuthentication
            f"Authenticate to log in as {user['name']}",
            reply_handler
        )
    except Exception as e:
        logging.error(f"Failed to initiate biometric: {e}")
        if reply_handler in BIOMETRIC_KEEP_ALIVE:
            BIOMETRIC_KEEP_ALIVE.remove(reply_handler)
        return jsonify({"success": False, "error": f"Failed to initiate Touch ID: {e}"})
        
    # Wait for the user to complete or cancel the dialog
    auth_event.wait()
    
    if auth_result["success"]:
        # Reset failed attempts
        session.pop(f"failed_attempts_{user_id}", None)
        # Log user in
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['is_admin'] = user['is_admin']
        flash(f"Welcome back, {user['name']}!")
        return jsonify({"success": True, "redirect": url_for('dashboard')})
    else:
        err_msg = auth_result.get("error") or "Biometric verification failed or was canceled."
        return jsonify({"success": False, "error": err_msg})

@app.route('/api/web/verify_action', methods=['POST'])
@login_required
@admin_required
def verify_action():
    """Generic Touch ID checkpoint for sensitive mutations."""
    reason = request.form.get('reason', 'Authenticate to proceed with this sensitive action.')
    password = request.form.get('password')
    
    if password:
        conn = get_db()
        user = conn.execute("SELECT password FROM users WHERE id = ?", (session.get('user_id'),)).fetchone()
        conn.close()
        if user and user['password'] and check_password_hash(user['password'], password):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Incorrect administrator password."})

    if not BIOMETRICS_AVAILABLE:
        # If hardware is incapable or disabled, allow administrators to proceed natively
        return jsonify({"success": True})
        
    global BIOMETRIC_KEEP_ALIVE
    import threading
    auth_event = threading.Event()
    auth_result = {"success": False, "error": None}
    
    def reply_handler(success, error):
        try:
            auth_result["success"] = success
            if error:
                auth_result["error"] = str(error.localizedDescription())
        except Exception as e:
            logging.error(f"Error in biometric reply handler: {e}")
        finally:
            auth_event.set()
            if reply_handler in BIOMETRIC_KEEP_ALIVE:
                BIOMETRIC_KEEP_ALIVE.remove(reply_handler)
                
    BIOMETRIC_KEEP_ALIVE.append(reply_handler)
    
    try:
        context = LAContext.alloc().init()
        context.evaluatePolicy_localizedReason_reply_(
            2, # LAPolicyDeviceOwnerAuthentication
            reason,
            reply_handler
        )
    except Exception as e:
        if reply_handler in BIOMETRIC_KEEP_ALIVE:
            BIOMETRIC_KEEP_ALIVE.remove(reply_handler)
        return jsonify({"success": False, "error": f"Failed to initiate hardware security: {e}"})
        
    auth_event.wait(timeout=30.0)
    if not auth_event.is_set():
        if reply_handler in BIOMETRIC_KEEP_ALIVE:
            BIOMETRIC_KEEP_ALIVE.remove(reply_handler)
        return jsonify({"success": False, "error": "Authentication timed out."})
        
    return jsonify(auth_result)


@app.route('/users/logout')
def logout_user():
    session.clear()
    flash("Logged out successfully.")
    return redirect(url_for('google_login_page'))


@app.route('/users/enroll', methods=['POST'])
def enroll_profiles():
    if not CURRENT_DB:
        return redirect(url_for('dashboard'))
        
    admin_name = request.form.get('admin_name', 'Admin').strip()
    if not admin_name:
        admin_name = 'Admin'
        
    raw_password = request.form.get('admin_password', '').strip()
    hashed_password = generate_password_hash(raw_password) if raw_password else ''
    
    file = request.files.get('admin_profile_picture')
    file_content = None
    if file and file.filename:
        file_content = file.read()
        
    conn = get_db()
    # Check if there is an Admin user already or any user
    admin_exists = conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchone()
    
    if admin_exists:
        # Update existing Admin user
        admin_id = admin_exists['id']
        if file_content:
            conn.execute("UPDATE users SET name = ?, password = ?, profile_picture_blob = ? WHERE id = ?",
                         (admin_name, hashed_password, sqlite3.Binary(file_content), admin_id))
        else:
            conn.execute("UPDATE users SET name = ?, password = ? WHERE id = ?",
                         (admin_name, hashed_password, admin_id))
    else:
        # Create new Admin user
        if file_content:
            cursor = conn.execute("INSERT INTO users (name, is_admin, password, profile_picture_blob) VALUES (?, 1, ?, ?)",
                                  (admin_name, hashed_password, sqlite3.Binary(file_content)))
        else:
            cursor = conn.execute("INSERT INTO users (name, is_admin, password) VALUES (?, 1, ?)",
                                  (admin_name, hashed_password))
        admin_id = cursor.lastrowid
        
    # Update system_info to set profiles_enrolled = '1'
    conn.execute("INSERT OR REPLACE INTO system_info (meta_field, meta_value) VALUES ('profiles_enrolled', '1')")
    conn.commit()
    
    # Get the exact admin info to log the user in
    admin_row = conn.execute("SELECT * FROM users WHERE is_admin = 1").fetchone()
    conn.close()
    
    # Set the active session to the enrolled administrator
    session['user_id'] = admin_row['id']
    session['user_name'] = admin_row['name']
    session['is_admin'] = 1
    
    flash("Congratulations! User Profiles are now successfully enabled and secured.")
    return redirect(url_for('manage_users'))


@app.route('/users/disenroll', methods=['POST'])
@admin_required
def disenroll_profiles():
    if not CURRENT_DB:
        return redirect(url_for('dashboard'))

    # Require the current admin's password to confirm this destructive action
    confirm_password = request.form.get('confirm_password', '').strip()
    if confirm_password:
        conn = get_db()
        user = conn.execute("SELECT password FROM users WHERE id = ?", (session.get('user_id'),)).fetchone()
        conn.close()
        if not user or not user['password'] or not check_password_hash(user['password'], confirm_password):
            flash("Incorrect password. User Profiles were NOT disabled.")
            return redirect(url_for('manage_users'))

    conn = get_db()
    # Set profiles_enrolled = '0'
    conn.execute("INSERT OR REPLACE INTO system_info (meta_field, meta_value) VALUES ('profiles_enrolled', '0')")
    conn.commit()
    conn.close()
    
    # Reset active session to System User bypass
    session['user_id'] = -1
    session['user_name'] = "System User"
    session['is_admin'] = 1
    session['current_db_path'] = get_db_path()  # Keep path in sync to avoid before_request wipe

    flash("User Profiles disabled. Authentication is no longer required.")
    return redirect(url_for('manage_users'))


@app.route('/users/avatar/<int:user_id>')
def get_user_avatar(user_id):
    default_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="100" height="100" style="background:#1e293b;"><circle cx="12" cy="12" r="12" fill="#334155"/><circle cx="12" cy="8" r="4" fill="#94a3b8"/><path d="M12 14c-4.42 0-8 2.24-8 5v1h16v-1c0-2.76-3.58-5-8-5z" fill="#94a3b8"/></svg>"""
    if not CURRENT_DB:
        return Response(default_svg, mimetype='image/svg+xml')
    conn = get_db()
    user = conn.execute("SELECT profile_picture_blob FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if user and user['profile_picture_blob']:
        return Response(user['profile_picture_blob'], mimetype='image/png')
    return Response(default_svg, mimetype='image/svg+xml')


@app.route('/users/manage')
@admin_required
def manage_users():
    conn = get_db()
    # Admins appear first, then alphabetically
    users = conn.execute(
        "SELECT id, name, is_admin, password, permissions, biometrics_enabled FROM users ORDER BY is_admin DESC, name ASC"
    ).fetchall()
    conn.close()
    users_annotated = []
    for u in users:
        d = dict(u)
        d['has_password'] = bool(d.get('password'))
        # Parse stored permissions JSON; default to all-access dict
        import json as _json
        try:
            d['perms'] = _json.loads(d.get('permissions') or '{}')
        except Exception:
            d['perms'] = {}
        users_annotated.append(d)
    return render_template('users_manage.html', active_page='users', users=users_annotated, biometrics_available=BIOMETRICS_AVAILABLE)


@app.route('/users/manage/add', methods=['POST'])
@admin_required
def add_user():
    name = request.form.get('name', '').strip()
    if not name:
        flash("User name is required.")
        return redirect(url_for('manage_users'))
    is_admin = int(request.form.get('is_admin') or 0)
    biometrics_enabled = int(request.form.get('biometrics_enabled') or 0)
    raw_password = request.form.get('password') or ''
    # Always hash non-empty passwords before storing
    hashed_password = generate_password_hash(raw_password) if raw_password else ''
    file = request.files.get('profile_picture')
    
    file_content = None
    if file and file.filename:
        file_content = file.read()

    conn = get_db()
    if file_content:
        conn.execute("INSERT INTO users (name, is_admin, password, biometrics_enabled, profile_picture_blob) VALUES (?, ?, ?, ?, ?)",
                     (name, is_admin, hashed_password, biometrics_enabled, sqlite3.Binary(file_content)))
    else:
        conn.execute("INSERT INTO users (name, is_admin, password, biometrics_enabled) VALUES (?, ?, ?, ?)",
                     (name, is_admin, hashed_password, biometrics_enabled))
    conn.commit()
    conn.close()
    flash("User created successfully.")
    return redirect(url_for('manage_users'))


@app.route('/users/manage/edit', methods=['POST'])
@admin_required
def edit_user():
    user_id = request.form.get('id')
    name = request.form.get('name', '').strip()
    is_admin = int(request.form.get('is_admin') or 0)
    biometrics_enabled = int(request.form.get('biometrics_enabled') or 0)
    raw_password = request.form.get('password', '').strip()
    file = request.files.get('profile_picture')
    
    # Prevent an active admin from accidentally revoking their own role
    if int(user_id) == session.get('user_id'):
        is_admin = session.get('is_admin')
    
    file_content = None
    if file and file.filename:
        file_content = file.read()

    conn = get_db()
    if raw_password:
        # Hash the new password before storing
        hashed_password = generate_password_hash(raw_password)
        if file_content:
            conn.execute(
                "UPDATE users SET name = ?, is_admin = ?, password = ?, biometrics_enabled = ?, profile_picture_blob = ? WHERE id = ?",
                (name, is_admin, hashed_password, biometrics_enabled, sqlite3.Binary(file_content), user_id)
            )
        else:
            conn.execute(
                "UPDATE users SET name = ?, is_admin = ?, password = ?, biometrics_enabled = ? WHERE id = ?",
                (name, is_admin, hashed_password, biometrics_enabled, user_id)
            )
    else:
        # No new password — keep existing hash
        if file_content:
            conn.execute(
                "UPDATE users SET name = ?, is_admin = ?, biometrics_enabled = ?, profile_picture_blob = ? WHERE id = ?",
                (name, is_admin, biometrics_enabled, sqlite3.Binary(file_content), user_id)
            )
        else:
            conn.execute(
                "UPDATE users SET name = ?, is_admin = ?, biometrics_enabled = ? WHERE id = ?",
                (name, is_admin, biometrics_enabled, user_id)
            )
    conn.commit()
    conn.close()
    
    # Keep session in sync if the admin edited their own active profile
    if int(user_id) == session.get('user_id'):
        session['user_name'] = name
        session['is_admin'] = is_admin

    flash("User updated successfully.")
    return redirect(url_for('manage_users'))


@app.route('/users/manage/delete/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    # Prevent self-deletion
    if user_id == session.get('user_id'):
        flash("You cannot delete your own active profile!")
        return redirect(url_for('manage_users'))

    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User deleted successfully.")
    return redirect(url_for('manage_users'))


@app.route('/users/manage/permissions', methods=['POST'])
@admin_required
def save_permissions():
    """Save per-user page access permissions as a JSON blob."""
    import json as _pjson
    user_id = request.form.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Missing user_id'}), 400

    # All known permission keys
    PERM_KEYS = ['reports', 'all_entries', 'demands', 'clients', 'areas', 'analytics', 'settings', 'users']
    perms = {k: (request.form.get(f'perm_{k}') == '1') for k in PERM_KEYS}

    conn = get_db()
    conn.execute("UPDATE users SET permissions = ? WHERE id = ?",
                 (_pjson.dumps(perms), int(user_id)))
    conn.commit()
    conn.close()
    flash("Permissions updated successfully.")
    return redirect(url_for('manage_users'))

def get_dashboard_data():
    if not CURRENT_DB:
        return {}
    
    uid = get_user_filter()
    conn = get_db()
    settings = get_settings(conn)
    fin = get_financial_sql_snippets(settings)
    
    valid_cond = f"(payment_status IS NULL OR payment_status NOT IN ('CANCELLED', 'BAD DEBT', 'Cancelled', 'Bad Debt', 'cancelled', 'bad debt')) AND (ownership_status IS NULL OR ownership_status NOT IN ('Not Owned', 'NOT OWNED', 'not owned')) AND user_id = {uid}"
    stats = {
        'revenue': conn.execute(f"SELECT SUM(total) FROM sales WHERE {valid_cond}").fetchone()[0] or 0,
        'unpaid': conn.execute(f"SELECT SUM(total) FROM sales WHERE payment_status = 'NOT PAID' AND user_id = ? AND NOT (UPPER(IFNULL(ownership_status, '')) = 'NOT OWNED' OR UPPER(IFNULL(payment_status, '')) = 'CANCELLED')", (uid,)).fetchone()[0] or 0,
        'clients': conn.execute("SELECT COUNT(*) FROM client_list WHERE delete_flag = 0 AND user_id = ?", (uid,)).fetchone()[0],
        'count': conn.execute(f"SELECT COUNT(*) FROM sales WHERE payment_status = 'NOT PAID' AND user_id = ? AND NOT (UPPER(IFNULL(ownership_status, '')) = 'NOT OWNED' OR UPPER(IFNULL(payment_status, '')) = 'CANCELLED')", (uid,)).fetchone()[0]
    }
    stats['profit'] = conn.execute(f"""
        SELECT SUM(profit) FROM (
            SELECT 
                {fin['profit_expr']} as profit
            FROM (
                SELECT total, investment_amount,
                       {fin['vat_expr']} as vat,
                       {fin['wht_expr']} as wht
                FROM sales s
                WHERE {valid_cond.replace('payment_status', 's.payment_status').replace('ownership_status', 's.ownership_status').replace('user_id', 's.user_id')}
            )
        )
    """).fetchone()[0] or 0
    
    # Expanded Analytics Data for Dashboard
    monthly = conn.execute(f"""
        SELECT 
            strftime('%Y-%m', completion_date) as m, 
            SUM(total) as r, 
            SUM(
                {fin['profit_expr']}
            ) as p, 
            COUNT(id) as c 
        FROM (
            SELECT s.*,
                   {fin['vat_expr']} as vat,
                   {fin['wht_expr']} as wht
            FROM sales s
            WHERE {valid_cond.replace('payment_status', 's.payment_status').replace('ownership_status', 's.ownership_status').replace('user_id', 's.user_id')}
        )
        GROUP BY m ORDER BY m DESC LIMIT 12
    """).fetchall()
    areas = conn.execute(f"SELECT a.name as n, COUNT(s.id) as c, SUM(s.total) as rev FROM sales s JOIN area_list a ON s.area_id = a.id WHERE {valid_cond.replace('payment_status', 's.payment_status').replace('ownership_status', 's.ownership_status').replace('user_id', 's.user_id')} GROUP BY n ORDER BY rev DESC LIMIT 10").fetchall()
    status_dist = conn.execute("SELECT payment_status, COUNT(*) as c, SUM(total) as t FROM sales WHERE user_id = ? GROUP BY payment_status", (uid,)).fetchall()
    top_clients = conn.execute(f"SELECT c.name as n, SUM(s.total) as rev FROM sales s JOIN client_list c ON s.client_id = c.id WHERE {valid_cond.replace('payment_status', 's.payment_status').replace('ownership_status', 's.ownership_status').replace('user_id', 's.user_id')} GROUP BY n ORDER BY rev DESC LIMIT 5").fetchall()
    contract_types = conn.execute(f"SELECT contract_type, COUNT(*) as c FROM sales WHERE contract_type IS NOT NULL AND contract_type != '' AND {valid_cond} GROUP BY contract_type").fetchall()
    gov_dist = conn.execute(f"SELECT is_gov, COUNT(*) as c FROM sales WHERE {valid_cond} GROUP BY is_gov").fetchall()
    companies = conn.execute(f"SELECT company_name, SUM(total) as rev FROM sales WHERE company_name IS NOT NULL AND {valid_cond} GROUP BY company_name ORDER BY rev DESC").fetchall()
    # URA Liability: VAT owed on invoices with ura_status=UNPAID and is_vat_rated=1
    ura_liability_row = conn.execute(f"""
        SELECT
            IFNULL(SUM(IFNULL(total, 0) * {fin['vat_rate']} / {100.0 + fin['vat_rate']}), 0) as vat_owed,
            COUNT(*) as invoice_count
        FROM sales
        WHERE ura_status = 'UNPAID' AND is_vat_rated = 1 AND user_id = ?
    """, (uid,)).fetchone()
    # Generate Proactive Alert Items
    action_items = []
    
    # 1. High-risk VAT due (Paid from client, but URA status is UNPAID)
    high_risk_rows = conn.execute(f"""
        SELECT s.entry_id, (IFNULL(s.total, 0) * {fin['vat_rate']} / {100.0 + fin['vat_rate']}) as vat
        FROM sales s
        WHERE s.payment_status = 'PAID' AND s.ura_status = 'UNPAID' AND s.is_vat_rated = 1
    """).fetchall()
    if high_risk_rows:
        total_risk_vat = sum(r['vat'] for r in high_risk_rows)
        curr_label = settings.get('currency', 'UGX')
        action_items.append({
            'icon': 'bi-exclamation-triangle-fill',
            'category': 'TAX RISK',
            'title': 'High Tax Risk: Unpaid URA VAT',
            'message': f"You have {len(high_risk_rows)} paid contract(s) with unpaid URA VAT totaling <strong>{curr_label} {total_risk_vat:,.0f}</strong>. Remit to URA to avoid penalties.",
            'link': '/reports?tab=ura&ura_filter=DUE',
            'style': {
                'bg': '#fdf2f2', 'border': '#fde8e8', 'text': '#e02424', 'icon_bg': '#fde8e8',
                'btn_class': 'btn-outline-danger'
            }
        })
        

        
    # 3. Missing Client TINs for active contracts
    missing_tins = conn.execute("""
        SELECT DISTINCT c.name
        FROM sales s
        JOIN client_list c ON s.client_id = c.id
        WHERE s.is_vat_rated = 1 AND (c.tin IS NULL OR TRIM(c.tin) = '') AND c.delete_flag = 0
    """).fetchall()
    if missing_tins:
        names = ", ".join(r['name'] for r in missing_tins[:3])
        if len(missing_tins) > 3:
            names += f" and {len(missing_tins) - 3} others"
        action_items.append({
            'icon': 'bi-person-badge-fill',
            'category': 'COMPLIANCE',
            'title': 'Missing Client TINs',
            'message': f"Tax return risk: {len(missing_tins)} client(s) ({names}) have active VAT-rated contracts but no TIN registered.",
            'link': '/clients',
            'style': {
                'bg': '#f0f9ff', 'border': '#e0f2fe', 'text': '#0284c7', 'icon_bg': '#e0f2fe',
                'btn_class': 'btn-outline-info'
            }
        })
        
    # 4. Monthly filing countdown deadline
    today = datetime.date.today()
    if today.day <= 15:
        days_left = 15 - today.day
        action_items.append({
            'icon': 'bi-calendar-event-fill',
            'category': 'DEADLINE',
            'title': 'URA Monthly VAT Filing Deadline',
            'message': f"The VAT filing deadline for previous month's return is in <strong>{days_left} day(s)</strong> (due on the 15th).",
            'link': '/reports?tab=ura',
            'style': {
                'bg': '#f5f3ff', 'border': '#ede9fe', 'text': '#4f46e5', 'icon_bg': '#ede9fe',
                'btn_class': 'btn-outline-primary'
            }
        })

    # 5. Missing Completion Dates
    missing_comp_date_rows = conn.execute("""
        SELECT COUNT(*) FROM sales
        WHERE (completion_date IS NULL OR TRIM(completion_date) = '')
          AND NOT (UPPER(IFNULL(ownership_status, '')) = 'NOT OWNED' OR UPPER(IFNULL(payment_status, '')) = 'CANCELLED')
    """).fetchone()[0]
    if missing_comp_date_rows:
        action_items.append({
            'icon': 'bi-calendar-x-fill',
            'category': 'DATA INTEGRITY',
            'title': 'Data Integrity: Missing Completion Dates',
            'message': f"You have <strong>{missing_comp_date_rows}</strong> active contract(s) missing a completion date. This will exclude them from monthly reports.",
            'link': '/reports?tab=entries',
            'style': {
                'bg': '#fdf2f2', 'border': '#fde8e8', 'text': '#e02424', 'icon_bg': '#fde8e8',
                'btn_class': 'btn-outline-danger'
            }
        })

    # 6. Duplicate Invoice Codes
    dup_invoices = conn.execute("""
        SELECT invoice_code, COUNT(*) as cnt
        FROM sales
        WHERE invoice_code IS NOT NULL AND TRIM(invoice_code) != '' AND TRIM(invoice_code) != '-'
        GROUP BY invoice_code HAVING cnt > 1
    """).fetchall()
    if dup_invoices:
        dup_names = ", ".join(f"'{r['invoice_code']}'" for r in dup_invoices[:3])
        if len(dup_invoices) > 3:
            dup_names += f" and {len(dup_invoices) - 3} others"
        action_items.append({
            'icon': 'bi-files',
            'category': 'ACCOUNTING',
            'title': 'Accounting Alert: Duplicate Invoice Numbers',
            'message': f"Duplicate invoice numbers detected for code(s) {dup_names}. Correct them to avoid audit flags.",
            'link': '/reports?tab=entries',
            'style': {
                'bg': '#fffbeb', 'border': '#fef3c7', 'text': '#d97706', 'icon_bg': '#fef3c7',
                'btn_class': 'btn-outline-warning'
            }
        })

    # 7. Duplicate PO Numbers
    dup_pos = conn.execute("""
        SELECT po_no, COUNT(*) as cnt
        FROM sales
        WHERE po_no IS NOT NULL AND TRIM(po_no) != '' AND TRIM(po_no) != '-' AND TRIM(po_no) != '0'
        GROUP BY po_no HAVING cnt > 1
    """).fetchall()
    if dup_pos:
        po_names = ", ".join(f"'{r['po_no']}'" for r in dup_pos[:3])
        if len(dup_pos) > 3:
            po_names += f" and {len(dup_pos) - 3} others"
        action_items.append({
            'icon': 'bi-file-earmark-spreadsheet-fill',
            'category': 'REGISTRY',
            'title': 'Registry Alert: Duplicate PO Numbers',
            'message': f"Multiple contracts share the same PO number(s): {po_names}. Verify if this is correct.",
            'link': '/reports?tab=entries',
            'style': {
                'bg': '#f0f9ff', 'border': '#e0f2fe', 'text': '#0284c7', 'icon_bg': '#e0f2fe',
                'btn_class': 'btn-outline-info'
            }
        })

    # 8. Missing Classifications (Supplying Company, Contract Type, Area)
    missing_class = conn.execute("""
        SELECT COUNT(*) FROM sales
        WHERE (company_name IS NULL OR TRIM(company_name) = '' OR contract_type IS NULL OR TRIM(contract_type) = '' OR area_id IS NULL)
          AND NOT (UPPER(IFNULL(ownership_status, '')) = 'NOT OWNED' OR UPPER(IFNULL(payment_status, '')) = 'CANCELLED')
    """).fetchone()[0]
    if missing_class:
        action_items.append({
            'icon': 'bi-folder-symlink-fill',
            'category': 'OPERATIONAL',
            'title': 'Operational Cleanup: Missing Classifications',
            'message': f"You have <strong>{missing_class}</strong> active contract(s) missing a Supplying Company, Contract Type, or Supply Area.",
            'link': '/reports?tab=entries',
            'style': {
                'bg': '#f5f3ff', 'border': '#ede9fe', 'text': '#4f46e5', 'icon_bg': '#ede9fe',
                'btn_class': 'btn-outline-primary'
            }
        })

    # YoY Comparison Data
    today = datetime.date.today()
    current_months = []
    for i in range(11, -1, -1):
        year = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year -= 1
        current_months.append(f"{year:04d}-{month:02d}")
    
    prior_months = []
    for m in current_months:
        y, mo = map(int, m.split('-'))
        prior_months.append(f"{y-1:04d}-{mo:02d}")
        
    yoy_rows = conn.execute(f"""
        SELECT 
            strftime('%Y-%m', completion_date) as m, 
            SUM(total) as r
        FROM sales
        WHERE {valid_cond.replace('payment_status', 'payment_status').replace('ownership_status', 'ownership_status')} AND completion_date IS NOT NULL AND completion_date != ''
        GROUP BY m
    """).fetchall()
    yoy_map = {row['m']: row['r'] for row in yoy_rows}
    
    yoy_current = [yoy_map.get(m, 0) for m in current_months]
    yoy_prior = [yoy_map.get(m, 0) for m in prior_months]
    
    month_names = []
    for m in current_months:
        y, mo = map(int, m.split('-'))
        month_names.append(datetime.date(y, mo, 1).strftime("%b"))
        
    yoy_current_label = f"{datetime.date(int(current_months[0].split('-')[0]), int(current_months[0].split('-')[1]), 1).strftime('%b %y')} - {datetime.date(int(current_months[-1].split('-')[0]), int(current_months[-1].split('-')[1]), 1).strftime('%b %y')}"
    yoy_prior_label = f"{datetime.date(int(prior_months[0].split('-')[0]), int(prior_months[0].split('-')[1]), 1).strftime('%b %y')} - {datetime.date(int(prior_months[-1].split('-')[0]), int(prior_months[-1].split('-')[1]), 1).strftime('%b %y')}"

    conn.close()
    
    stats['ura_unpaid_vat'] = round(ura_liability_row['vat_owed']) if ura_liability_row else 0
    stats['ura_unpaid_count'] = ura_liability_row['invoice_count'] if ura_liability_row else 0

    return {
        'stats': stats,
        'chart_labels': [r['m'] for r in monthly[::-1]],
        'chart_data': [r['r'] for r in monthly[::-1]],
        'yoy_labels': month_names,
        'yoy_current': yoy_current,
        'yoy_prior': yoy_prior,
        'yoy_current_label': yoy_current_label,
        'yoy_prior_label': yoy_prior_label,
        'profit_data': [r['p'] for r in monthly[::-1]],
        'monthly_counts': [r['c'] for r in monthly[::-1]],
        'area_labels': [r['n'] for r in areas],
        'area_data': [r['c'] for r in areas],
        'area_rev': [r['rev'] for r in areas],
        'status_labels': [r['payment_status'] for r in status_dist],
        'status_data': [r['c'] for r in status_dist],
        'status_values': [r['t'] for r in status_dist],
        'client_labels': [r['n'] for r in top_clients],
        'client_data': [r['rev'] for r in top_clients],
        'type_labels': [r['contract_type'] for r in contract_types],
        'type_data': [r['c'] for r in contract_types],
        'gov_labels': ['Government' if r['is_gov'] else 'Private' for r in gov_dist],
        'gov_data': [r['c'] for r in gov_dist],
        'company_labels': [r['company_name'] for r in companies],
        'company_rev': [r['rev'] for r in companies],
        'action_items': action_items
    }

@app.route('/')
def dashboard():
    if not session.get('user_id'):
        return redirect(url_for('google_login_page'))
    if not CURRENT_DB:
        return redirect(url_for('google_login_page'))
    data = get_dashboard_data()
    return render_template('dashboard.html', active_page='dashboard', **data)

@app.route('/api/dashboard_stats')
def api_dashboard_stats():
    if not CURRENT_DB:
        return jsonify({'error': 'No database open'}), 400
    return jsonify(get_dashboard_data())

@app.route('/api/alerts')
def api_alerts():
    """Returns action items for the notification drawer. Fetched on demand."""
    if not CURRENT_DB:
        return jsonify({'alerts': [], 'count': 0})
    data = get_dashboard_data()
    alerts = data.get('action_items', [])
    return jsonify({'alerts': alerts, 'count': len(alerts)})

@app.route('/sales')
def sales():
    conn = get_db()
    cs = conn.execute("SELECT * FROM client_list WHERE delete_flag = 0 AND user_id = ?", (get_user_filter(),)).fetchall()
    as_ = conn.execute("SELECT * FROM area_list WHERE delete_flag = 0 AND user_id = ? ORDER BY name COLLATE NOCASE", (get_user_filter(),)).fetchall()
    last = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    next_id = f"{ (last[0] if last else 0) + 1 :03d}"
    settings = get_settings()
    return render_template('sales.html', active_page='sales', clients=cs, areas=as_, next_id=next_id, is_edit=False, sale={}, settings=settings)



@app.route('/sales/add', methods=['POST'])
def add_sale():
    f = request.form
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO sales (entry_id, invoice_code, contract_no, po_no, client_id, area_id, completion_date, contract_details, company_name, contract_type, is_gov, is_vat_rated, payment_status, payment_date, ura_status, ownership_status, total, investment_amount, tax_invoice_date, tax_period) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (f.get('entry_id'), f.get('invoice'), f.get('contract_no'), f.get('po_no'), f.get('client_id'), f.get('area_id'), f.get('completion_date'), f.get('contract_details'), f.get('company_name'), f.get('contract_type'), int(f.get('is_gov') or 0), int(f.get('is_vat') or 0), f.get('payment_status'), f.get('payment_date'), f.get('ura_status'), f.get('ownership_status', 'Owned'), safe_float(f.get('amount')), None if (f.get('investment') is None or str(f.get('investment')).strip() == '') else safe_float(f.get('investment')), f.get('tax_invoice_date') or None, f.get('tax_period') if f.get('ura_status') in ['PAID', 'OFFSET'] else None))
        new_id = cursor.lastrowid
        conn.commit()
        last = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
        next_id = f"{ (last[0] if last else 0) + 1 :03d}"
        conn.close()
        if f.get('ajax') == '1':
            return jsonify({"success": True, "id": new_id, "next_id": next_id})
        flash("Contract added successfully.")
        if f.get('action') == 'save_new':
            return redirect(f"{f.get('current_url', url_for('demands'))}?open_modal=true")
        return redirect(url_for('demands'))
    except Exception as e:
        if f.get('ajax') == '1':
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f"Error saving: {e}")
        return redirect(url_for('demands'))

@app.route('/demands')
def demands():
    c_id = request.args.get('client_id'); a_id = request.args.get('area_id'); status = request.args.get('status')
    where = " AND NOT (UPPER(IFNULL(s.payment_status,'')) IN ('CANCELLED','BAD DEBT') OR UPPER(IFNULL(s.ownership_status,'')) = 'NOT OWNED')"
    params = []
    if c_id: where += " AND s.client_id = ?"; params.append(c_id)
    if a_id: where += " AND s.area_id = ?"; params.append(a_id)
    if status: where += " AND s.payment_status = ?"; params.append(status)
    
    conn = get_db()
    cs = conn.execute("SELECT * FROM client_list WHERE delete_flag = 0 AND user_id = ?", (get_user_filter(),)).fetchall()
    as_ = conn.execute("SELECT * FROM area_list WHERE delete_flag = 0 AND user_id = ? ORDER BY name COLLATE NOCASE", (get_user_filter(),)).fetchall()
    conn.close()
    
    return render_template('demands.html', active_page='demands', demands=get_sales_query(where, params), clients=cs, areas=as_)

@app.route('/api/sales/datatables')
def sales_datatables():
    draw = request.args.get('draw', type=int)
    start = request.args.get('start', type=int) or 0
    length = request.args.get('length', type=int) or 50
    search_value = request.args.get('search[value]', '')
    
    conn = get_db()
    total_records = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    
    where = " AND NOT (UPPER(IFNULL(s.payment_status,'')) IN ('CANCELLED','BAD DEBT') OR UPPER(IFNULL(s.ownership_status,'')) = 'NOT OWNED')"
    params = []
    
    c_id = request.args.get('client_id'); a_id = request.args.get('area_id'); status = request.args.get('status')
    if c_id: where += " AND s.client_id = ?"; params.append(c_id)
    if a_id: where += " AND s.area_id = ?"; params.append(a_id)
    if status: where += " AND s.payment_status = ?"; params.append(status)
    
    if search_value:
        w = f"%{search_value}%"
        where += """ AND (
            s.invoice_code LIKE ? OR s.contract_no LIKE ? OR s.po_no LIKE ?
            OR s.entry_id LIKE ? OR s.contract_details LIKE ?
            OR s.company_name LIKE ? OR s.payment_status LIKE ?
            OR c.name LIKE ? OR a.name LIKE ?
        )"""
        params.extend([w, w, w, w, w, w, w, w, w])
        
    filtered_records = conn.execute(f"""
        SELECT COUNT(*) FROM sales s 
        LEFT JOIN client_list c ON s.client_id = c.id 
        LEFT JOIN area_list a ON s.area_id = a.id 
        WHERE 1=1 {where}
    """, params).fetchone()[0]
    
    where += " LIMIT ? OFFSET ?"
    params.extend([length, start])
    
    data = get_sales_query(where, params)
    
    return jsonify({
        "draw": draw,
        "recordsTotal": total_records,
        "recordsFiltered": filtered_records,
        "data": data
    })

@app.route('/all_entries')
def all_entries():
    conn = get_db()
    cs = conn.execute("SELECT * FROM client_list WHERE delete_flag = 0 AND user_id = ?", (get_user_filter(),)).fetchall()
    as_ = conn.execute("SELECT * FROM area_list WHERE delete_flag = 0 AND user_id = ? ORDER BY name COLLATE NOCASE", (get_user_filter(),)).fetchall()
    conn.close()
    
    return render_template('all_entries.html', active_page='all_entries', demands=[], clients=cs, areas=as_)

@app.route('/api/all_entries_data')
def api_all_entries_data():
    """Paginated JSON API for the All Entries Registry. Supports server-side
    filtering and offset-based pagination to keep initial load fast."""
    from flask import jsonify
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)
    per_page = min(per_page, 500)  # Safety cap
    
    # Build filters from request params
    c_id = request.args.get('client_id')
    a_id = request.args.get('area_id')
    status = request.args.get('status')
    company = request.args.get('company')
    c_type = request.args.get('contract_type')
    ownership = request.args.get('ownership')
    ura_status = request.args.get('ura_status')
    start = request.args.get('start_date')
    end = request.args.get('end_date')
    min_amount = request.args.get('min_amount')
    max_amount = request.args.get('max_amount')
    search = request.args.get('search', '').strip()
    
    filters = ""; params = []
    if c_id: filters += " AND s.client_id = ?"; params.append(c_id)
    if a_id: filters += " AND s.area_id = ?"; params.append(a_id)
    if status: filters += " AND s.payment_status = ?"; params.append(status)
    if ownership: filters += " AND s.ownership_status = ?"; params.append(ownership)
    if ura_status: filters += " AND s.ura_status = ?"; params.append(ura_status)
    if start: filters += " AND s.completion_date >= ?"; params.append(start)
    if end: filters += " AND s.completion_date <= ?"; params.append(end)
    if c_type: filters += " AND s.contract_type = ?"; params.append(c_type)
    if search:
        search_like = f"%{search}%"
        filters += " AND (s.contract_details LIKE ? OR s.contract_no LIKE ? OR s.invoice_code LIKE ? OR s.po_no LIKE ? OR s.entry_id LIKE ? OR c.name LIKE ? OR a.name LIKE ? OR s.company_name LIKE ?)"
        params.extend([search_like] * 8)
    if min_amount:
        try: filters += " AND s.total >= ?"; params.append(float(min_amount))
        except ValueError: pass
    if max_amount:
        try: filters += " AND s.total <= ?"; params.append(float(max_amount))
        except ValueError: pass
    
    # Count total matching records (for pagination metadata)
    count_query = f'''SELECT COUNT(*) FROM sales s 
        LEFT JOIN client_list c ON s.client_id = c.id 
        LEFT JOIN area_list a ON s.area_id = a.id 
        WHERE 1=1 {filters}'''
    
    conn = get_db()
    settings = get_settings(conn)
    fin = get_financial_sql_snippets(settings)

    # Full query with computed columns + pagination
    data_query = f'''
        SELECT *, 
               (IFNULL(total, 0) - (IFNULL(vat, 0) + wht)) as net_amount,
               (IFNULL(total, 0) - (wht * 2)) as net_payable,
               {fin['profit_expr']} as profit
        FROM (
            SELECT s.*, c.name as client_name, c.contact as client_contact, a.name as area_name,
                   {fin['vat_expr']} as vat,
                   {fin['wht_expr']} as wht
            FROM sales s 
            LEFT JOIN client_list c ON s.client_id = c.id 
            LEFT JOIN area_list a ON s.area_id = a.id 
            WHERE 1=1 {filters}
        ) ORDER BY completion_date DESC, id DESC
        LIMIT ? OFFSET ?'''
    
    # Aggregates query for KPI stats (runs over ALL matching rows, not just current page)
    agg_query = f'''
        SELECT 
            IFNULL(SUM(CASE WHEN upper(payment_status) NOT IN ('BAD DEBT','CANCELLED') AND upper(IFNULL(ownership_status,'')) != 'NOT OWNED' THEN total ELSE 0 END), 0) as total_revenue,
            IFNULL(SUM(CASE WHEN upper(payment_status) NOT IN ('BAD DEBT','CANCELLED') AND upper(IFNULL(ownership_status,'')) != 'NOT OWNED' THEN 
                CASE WHEN investment_amount IS NULL THEN {fin['profit_margin']} * (IFNULL(total, 0) - (IFNULL(vat_calc, 0) + wht_calc))
                     ELSE ((IFNULL(total, 0) - (IFNULL(vat_calc, 0) + wht_calc)) - investment_amount) END
            ELSE 0 END), 0) as total_profit,
            IFNULL(SUM(CASE WHEN payment_status = 'PAID' AND upper(payment_status) NOT IN ('BAD DEBT','CANCELLED') AND upper(IFNULL(ownership_status,'')) != 'NOT OWNED' THEN total ELSE 0 END), 0) as total_collected,
            IFNULL(SUM(CASE WHEN payment_status != 'PAID' AND upper(payment_status) NOT IN ('BAD DEBT','CANCELLED') AND upper(IFNULL(ownership_status,'')) != 'NOT OWNED' THEN total ELSE 0 END), 0) as total_outstanding
        FROM (
            SELECT s.*, 
                   (IFNULL(s.total, 0) * {fin['vat_rate']} / {100.0 + fin['vat_rate']} * IFNULL(s.is_vat_rated, 0)) as vat_calc,
                   ((IFNULL(s.total, 0) - (IFNULL(s.total, 0) * {fin['vat_rate']} / {100.0 + fin['vat_rate']} * IFNULL(s.is_vat_rated, 0))) * {fin['wht_rate'] / 100.0} * CASE WHEN (IFNULL(s.is_gov, 0) = 1 OR IFNULL(s.is_vat_rated, 0) = 1) THEN 1 ELSE 0 END) as wht_calc
            FROM sales s 
            LEFT JOIN client_list c ON s.client_id = c.id 
            LEFT JOIN area_list a ON s.area_id = a.id 
            WHERE 1=1 {filters}
        )'''
    
    conn = get_db()
    total_count = conn.execute(count_query, params).fetchone()[0]
    
    offset = (page - 1) * per_page
    data_params = params + [per_page, offset]
    rows = conn.execute(data_query, data_params).fetchall()
    
    agg = conn.execute(agg_query, params).fetchone()
    conn.close()
    
    # Format dates for JSON (replicate Jinja filter logic)
    def fmt_date(d):
        if not d: return "-"
        try:
            from datetime import datetime
            for f in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d-%m-%Y', '%d/%m/%Y'):
                try: return datetime.strptime(str(d)[:19], f).strftime('%d-%b-%Y')
                except ValueError: continue
            return str(d)
        except: return str(d)
    
    def fmt_form_date(d):
        if not d: return ""
        try:
            s = str(d).split(' ')[0]
            from datetime import datetime
            try:
                datetime.strptime(s, '%Y-%m-%d')
                return s
            except ValueError:
                for f in ('%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
                    try: return datetime.strptime(str(d), f).strftime('%Y-%m-%d')
                    except ValueError: continue
            return ""
        except: return ""
    
    def fmt_no_decimal(v):
        if v is None: return ""
        s = str(v)
        if '.' in s:
            parts = s.split('.')
            if parts[1].strip('0') == '': return parts[0]
        return s
    
    items = []
    for r in rows:
        d = dict(r)
        items.append({
            'id': d.get('id'),
            'entry_id': d.get('entry_id', ''),
            'contract_details': d.get('contract_details', ''),
            'completion_date': fmt_date(d.get('completion_date')),
            'completion_date_raw': fmt_form_date(d.get('completion_date')),
            'contract_no': d.get('contract_no', ''),
            'po_no': fmt_no_decimal(d.get('po_no')),
            'invoice_code': fmt_no_decimal(d.get('invoice_code')),
            'client_name': d.get('client_name', ''),
            'client_id': d.get('client_id', ''),
            'area_name': d.get('area_name', ''),
            'company_name': d.get('company_name', ''),
            'contract_type': d.get('contract_type', ''),
            'ownership_status': d.get('ownership_status', ''),
            'is_gov': bool(d.get('is_gov')),
            'is_vat_rated': bool(d.get('is_vat_rated')),
            'payment_status': d.get('payment_status', ''),
            'payment_date': fmt_date(d.get('payment_date')),
            'ura_status': d.get('ura_status', ''),
            'total': d.get('total') or 0,
            'investment_amount': d.get('investment_amount') or 0,
            'vat': d.get('vat') or 0,
            'net_payable': d.get('net_payable') or 0,
            'profit': d.get('profit') or 0,
        })
    
    return jsonify({
        'items': items,
        'page': page,
        'per_page': per_page,
        'total': total_count,
        'has_more': offset + per_page < total_count,
        'stats': {
            'revenue': round(agg[0]),
            'profit': round(agg[1]),
            'collected': round(agg[2]),
            'outstanding': round(agg[3]),
        }
    })

def get_filtered_sales_from_request():
    # Primary Filters
    c_id = request.args.get('client_id'); a_id = request.args.get('area_id'); status = request.args.get('status')
    # Additional Advanced Filters
    company = request.args.get('company'); c_type = request.args.get('contract_type')
    ownership = request.args.get('ownership'); ura = request.args.get('ura')
    start = request.args.get('start_date'); end = request.args.get('end_date')
    min_amount = request.args.get('min_amount')
    max_amount = request.args.get('max_amount')
    
    filters = ""; params = []
    if c_id: filters += " AND s.client_id = ?"; params.append(c_id)
    if a_id: filters += " AND s.area_id = ?"; params.append(a_id)
    if status: filters += " AND s.payment_status = ?"; params.append(status)
    if ownership: filters += " AND s.ownership_status = ?"; params.append(ownership)
    if start: filters += " AND s.completion_date >= ?"; params.append(start)
    if end: filters += " AND s.completion_date <= ?"; params.append(end)
    if c_type: filters += " AND s.contract_type = ?"; params.append(c_type)
    if min_amount:
        try:
            filters += " AND s.total >= ?"
            params.append(float(min_amount))
        except ValueError:
            pass
    if max_amount:
        try:
            filters += " AND s.total <= ?"
            params.append(float(max_amount))
        except ValueError:
            pass
            
    return get_sales_query(filters, params)

@app.route('/reports')
def reports():
    # Payment date range parameters
    pay_start = request.args.get('pay_start')
    pay_end = request.args.get('pay_end')
    
    if pay_start is None and pay_end is None:
        # Default to current month on initial load
        today = datetime.date.today()
        pay_start = datetime.date(today.year, today.month, 1).strftime("%Y-%m-%d")
        if today.month == 12:
            pay_end = datetime.date(today.year, 12, 31).strftime("%Y-%m-%d")
        else:
            pay_end = (datetime.date(today.year, today.month + 1, 1) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        
    sales = get_filtered_sales_from_request()
    active_sales = [s for s in sales if s.get('payment_status','').upper() not in ('BAD DEBT','CANCELLED') and s.get('ownership_status','').upper() != 'NOT OWNED']
    summary = {
        'revenue': sum(s['total'] for s in active_sales), 
        'paid': sum(s['total'] for s in active_sales if s['payment_status']=='PAID'), 
        'unpaid': sum(s['total'] for s in active_sales if s['payment_status']=='NOT PAID'), 
        'profit': sum(s['profit'] for s in active_sales)
    }
    
    conn = get_db()
    cs = conn.execute("SELECT * FROM client_list WHERE delete_flag = 0 AND user_id = ?", (get_user_filter(),)).fetchall()
    as_ = conn.execute("SELECT * FROM area_list WHERE delete_flag = 0 AND user_id = ? ORDER BY name COLLATE NOCASE", (get_user_filter(),)).fetchall()
    # Fetch all unique contract types
    types_res = conn.execute("SELECT DISTINCT contract_type FROM sales WHERE contract_type IS NOT NULL AND contract_type != ''").fetchall()
    all_types = [t[0] for t in types_res]
    conn.close()
    
    # Calculate payments made in the selected payment period (for Tab 2)
    selected_month_payments = []
    for s in sales:
        if s.get('payment_status') == 'PAID':
            p_date = s.get('payment_date') or s.get('completion_date') or ''
            if p_date:
                if pay_start and p_date < pay_start:
                    continue
                if pay_end and p_date > pay_end:
                    continue
                selected_month_payments.append(s)
                
    selected_month_payments.sort(key=lambda x: x.get('payment_date') or '', reverse=True)
    payments_total = sum(s['total'] for s in selected_month_payments)
    payments_profit = sum(s['profit'] for s in selected_month_payments)
    payments_count = len(selected_month_payments)

    # Tax filing summary (VAT-rated entries only) - complete period totals and breakdowns
    payments_vat_total = sum(float(s.get('vat') or 0) for s in selected_month_payments if s.get('is_vat_rated'))
    payments_wht_total = sum(float(s.get('wht') or 0) for s in selected_month_payments)
    payments_vat_count = sum(1 for s in selected_month_payments if s.get('is_vat_rated'))
    
    # Settlement breakdowns
    payments_vat_paid = sum(float(s.get('vat') or 0) for s in selected_month_payments if s.get('is_vat_rated') and s.get('ura_status') == 'PAID')
    payments_vat_offset = sum(float(s.get('vat') or 0) for s in selected_month_payments if s.get('is_vat_rated') and s.get('ura_status') == 'OFFSET')
    payments_vat_outstanding = sum(float(s.get('vat') or 0) for s in selected_month_payments if s.get('is_vat_rated') and s.get('ura_status') not in ('OFFSET', 'PAID'))
    payments_wht_outstanding = sum(float(s.get('wht') or 0) for s in selected_month_payments if s.get('ura_status') not in ('OFFSET', 'PAID'))
    
    
    # Format range dates for UI display
    pay_start_formatted = pay_start
    pay_end_formatted = pay_end
    try:
        pay_start_formatted = datetime.datetime.strptime(pay_start, "%Y-%m-%d").strftime("%d %b %Y")
        pay_end_formatted = datetime.datetime.strptime(pay_end, "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        pass
        
    # Calculate demands (active unpaid sales)
    demand_sales = [s for s in active_sales if s.get('payment_status') != 'PAID']
    demand_total = sum(s['total'] for s in demand_sales)
    demand_profit = sum(s['profit'] for s in demand_sales)
    demand_count = len(demand_sales)

    # Get only clients that have outstanding demands
    demanded_client_ids = set(s['client_id'] for s in demand_sales if s.get('client_id'))
    
    # Sum unpaid totals per client
    client_demanded_totals = {}
    for s in demand_sales:
        cid = s.get('client_id')
        if cid:
            client_demanded_totals[cid] = client_demanded_totals.get(cid, 0.0) + float(s.get('total') or 0)

    demanded_clients = []
    for c in cs:
        if c['id'] in demanded_client_ids:
            c_dict = dict(c)
            c_dict['total_demanded'] = client_demanded_totals.get(c['id'], 0.0)
            demanded_clients.append(c_dict)

    # Sort descending by outstanding demanded amount
    demanded_clients.sort(key=lambda x: x['total_demanded'], reverse=True)

    # URA Section: all VAT-rated sales, ordered by urgency (UNPAID first)
    ura_sales = [s for s in sales if s.get('is_vat_rated')]
    ura_sales_ordered = sorted(
        ura_sales,
        key=lambda x: x.get('tax_invoice_date') or '',
        reverse=True
    )
    ura_summary = {
        'unpaid_due_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'UNPAID' and s.get('payment_status') == 'PAID'),
        'unpaid_due_count': sum(1 for s in ura_sales if s.get('ura_status') == 'UNPAID' and s.get('payment_status') == 'PAID'),
        'unpaid_not_due_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'UNPAID' and s.get('payment_status') != 'PAID'),
        'unpaid_not_due_count': sum(1 for s in ura_sales if s.get('ura_status') == 'UNPAID' and s.get('payment_status') != 'PAID'),
        'paid_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'PAID'),
        'paid_count': sum(1 for s in ura_sales if s.get('ura_status') == 'PAID'),
        'offset_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'OFFSET'),
        'offset_count': sum(1 for s in ura_sales if s.get('ura_status') == 'OFFSET'),
        'not_fileable_count': sum(1 for s in ura_sales if s.get('ura_status') == 'NOT FILEABLE'),
        'total_vat': sum((s.get('vat') or 0) for s in ura_sales),
        'unpaid_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'UNPAID'),
        'unpaid_count': sum(1 for s in ura_sales if s.get('ura_status') == 'UNPAID'),
    }

    # Calculate URA filing countdown (deadline is the 15th of current month for previous month's return)
    # Countdown starts on 1st of the current month until 15th.
    today = datetime.date.today()
    if today.day <= 15:
        # Active filing for previous month (e.g. if today is Aug 5, active period is July)
        if today.month == 1:
            active_period_date = datetime.date(today.year - 1, 12, 1)
        else:
            active_period_date = datetime.date(today.year, today.month - 1, 1)
        
        current_deadline = datetime.date(today.year, today.month, 15)
        days_remaining = (current_deadline - today).days
        countdown_active = True
        current_period = active_period_date.strftime("%B %Y")
    else:
        # No active countdown yet (next one for current month starts on 1st of next month)
        countdown_active = False
        days_remaining = 0
        current_period = today.strftime("%B %Y")
        if today.month == 12:
            current_deadline = datetime.date(today.year + 1, 1, 15)
        else:
            current_deadline = datetime.date(today.year, today.month + 1, 15)

    overdue_periods = []
    for s in ura_sales:
        # Only mention in overdue if it is UNPAID and DUE (payment_status == 'PAID')
        if s.get('ura_status') == 'UNPAID' and s.get('payment_status') == 'PAID':
            date_str = s.get('tax_invoice_date') or s.get('completion_date') or ''
            if date_str:
                try:
                    dt = datetime.datetime.strptime(date_str.split(' ')[0], "%Y-%m-%d").date()
                    if dt.month == 12:
                        deadline = datetime.date(dt.year + 1, 1, 15)
                    else:
                        deadline = datetime.date(dt.year, dt.month + 1, 15)
                        
                    if deadline < today:
                        period_name = dt.strftime("%B %Y")
                        if period_name not in overdue_periods:
                            overdue_periods.append(period_name)
                except ValueError:
                    pass
                    
    try:
        overdue_periods.sort(key=lambda x: datetime.datetime.strptime(x, "%B %Y"))
    except:
        pass

    # Calculate is_high_risk for each URA sale (unpaid and due for over 30 days)
    for s in ura_sales:
        is_high_risk = False
        if s.get('payment_status') == 'PAID' and s.get('ura_status') == 'UNPAID':
            pay_date_str = s.get('payment_date') or s.get('completion_date') or ''
            if pay_date_str:
                try:
                    pay_date = datetime.datetime.strptime(pay_date_str.split(' ')[0], "%Y-%m-%d").date()
                    days_due = (today - pay_date).days
                    if days_due > 30:
                        is_high_risk = True
                except ValueError:
                    pass
        s['is_high_risk'] = is_high_risk

    countdown_info = {
        'active': countdown_active,
        'days_remaining': days_remaining,
        'deadline_date': current_deadline.strftime("%d %b %Y"),
        'current_period': current_period,
        'overdue_periods': overdue_periods
    }

    return render_template('reports.html', active_page='reports', 
                           sales=sales, 
                           summary=summary, 
                           clients=cs, 
                           areas=as_, 
                           all_types=all_types, 
                           selected_month_payments=selected_month_payments,
                           payments_total=payments_total,
                           payments_profit=payments_profit,
                           payments_count=payments_count,
                           payments_vat_total=payments_vat_total,
                           payments_wht_total=payments_wht_total,
                           payments_vat_count=payments_vat_count,
                           payments_vat_paid=payments_vat_paid,
                           payments_vat_offset=payments_vat_offset,
                           payments_vat_outstanding=payments_vat_outstanding,
                           payments_wht_outstanding=payments_wht_outstanding,
                           pay_start=pay_start,
                           pay_end=pay_end,
                           pay_start_formatted=pay_start_formatted,
                           pay_end_formatted=pay_end_formatted,
                           demand_sales=demand_sales,
                           demand_total=demand_total,
                           demand_profit=demand_profit,
                           demand_count=demand_count,
                           demanded_clients=demanded_clients,
                           ura_sales=ura_sales_ordered,
                           ura_summary=ura_summary,
                           countdown_info=countdown_info)

@app.route('/generate_demand_note/<int:sale_id>')
def generate_demand_note(sale_id):
    sale = get_sales_query(" AND s.id = ?", [sale_id])
    if not sale: return "Not found", 404
    sale = sale[0]
    is_ghost = sale.get('payment_status','').upper() == 'CANCELLED' or sale.get('ownership_status','').upper() == 'NOT OWNED'
    if is_ghost:
        return "Access Denied: Cannot generate a demand note for a ghost entry.", 403
    if sale.get('payment_status','').upper() == 'BAD DEBT':
        return "Access Denied: Cannot generate a demand note for a bad debt entry.", 403
    settings = get_settings()
    pdf = DemandNotePDF(settings)
    pdf.add_page()
    
    # Title and Reference
    pdf.set_font('helvetica', 'B', 20)
    pdf.set_text_color(13, 110, 253)
    pdf.cell(0, 10, 'DEMAND NOTE', ln=1, align='R')
    pdf.set_font('helvetica', '', 10)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(0, 6, f"Reference: {sale['invoice_code']}", ln=1, align='R')
    pdf.cell(0, 6, f"Date: {sale['completion_date']}", ln=1, align='R')
    pdf.ln(10)
    
    # Bill To / Details Row
    y_top = pdf.get_y()
    pdf.draw_section_title("Bill To")
    pdf.draw_billing_row("Client:", sale['client_name'], True)
    if sale['client_contact']: pdf.draw_billing_row("Contact:", sale['client_contact'])
    pdf.draw_billing_row("Area:", sale['area_name'])
    
    pdf.set_xy(120, y_top)
    pdf.draw_section_title("Contract Info")
    pdf.set_x(120); pdf.draw_billing_row("Contract No:", sale['contract_no'] or "-")
    pdf.set_x(120); pdf.draw_billing_row("PO No:", sale['po_no'] or "-")
    pdf.set_x(120); pdf.draw_billing_row("Type:", sale['contract_type'])
    
    pdf.ln(15)
    
    # Table
    pdf.set_fill_color(30, 41, 59); pdf.set_text_color(255, 255, 255); pdf.set_font('helvetica', 'B', 11)
    pdf.cell(140, 10, ' DESCRIPTION', border=0, fill=True)
    curr_label = settings.get('currency', 'UGX')
    pdf.cell(50, 10, f'AMOUNT ({curr_label}) ', border=0, fill=True, align='R', ln=1)
    
    pdf.set_text_color(30, 41, 59); pdf.set_font('helvetica', '', 11)
    pdf.set_fill_color(248, 250, 252)
    
    # Zebra row content
    start_y = pdf.get_y()
    pdf.multi_cell(140, 10, sale['contract_details'], border=0, fill=True)
    end_y = pdf.get_y()
    pdf.set_xy(150, start_y)
    pdf.cell(50, (end_y - start_y), f"{(sale['total'] or 0):,.0f}", border=0, fill=True, align='R', ln=1)
    
    pdf.ln(10)
    
    # Totals Section
    pdf.set_x(120)
    pdf.set_font('helvetica', 'B', 10)
    pdf.cell(40, 8, "Subtotal:", border='B')
    pdf.cell(30, 8, f"{(sale['net_amount'] or 0):,.0f}", border='B', align='R', ln=1)
    
    pdf.set_x(120)
    vat_label = f"VAT ({settings.get('vat_rate', 18.0):g}%):"
    pdf.cell(40, 8, vat_label, border='B')
    pdf.cell(30, 8, f"{(sale['vat'] or 0):,.0f}", border='B', align='R', ln=1)
    
    if sale['wht'] > 0:
        pdf.set_x(120)
        pdf.set_text_color(220, 38, 38) # Red 600
        wht_label = f"WHT ({settings.get('wht_rate', 6.0):g}%):"
        pdf.cell(40, 8, wht_label, border='B')
        pdf.cell(30, 8, f"-{(sale['wht'] or 0):,.0f}", border='B', align='R', ln=1)
        pdf.set_text_color(30, 41, 59)
    
    pdf.set_x(120)
    pdf.set_font('helvetica', 'B', 12)
    pdf.set_fill_color(13, 110, 253); pdf.set_text_color(255, 255, 255)
    pdf.cell(40, 12, " NET PAYABLE", fill=True)
    pdf.cell(30, 12, f"{(sale['net_payable'] or 0):,.0f} ", fill=True, align='R', ln=1)
    
    pdf.ln(15)
    
    # Payment Details
    pdf.set_text_color(30, 41, 59)
    pdf.draw_section_title("Payment Instructions")
    pdf.set_font('helvetica', 'B', 10)
    pdf.cell(40, 7, "Bank Name:")
    pdf.set_font('helvetica', '', 10)
    pdf.cell(0, 7, settings.get('bank_name') or "N/A", ln=1)
    
    pdf.set_font('helvetica', 'B', 10)
    pdf.cell(40, 7, "Account Name:")
    pdf.set_font('helvetica', '', 10)
    pdf.cell(0, 7, settings.get('bank_account_name') or "N/A", ln=1)
    
    pdf.set_font('helvetica', 'B', 10)
    pdf.cell(40, 7, "Account Number:")
    pdf.set_font('helvetica', '', 10)
    pdf.cell(0, 7, settings.get('bank_account_number') or "N/A", ln=1)
    
    pdf.ln(10)
    pdf.set_font('helvetica', 'I', 9); pdf.set_text_color(100, 116, 139)
    pdf.cell(0, 10, "This is a computer generated document and does not require a signature.", align='C', ln=1)
    
    out = io.BytesIO(); pdf.output(out); out.seek(0)
    return send_file(out, as_attachment=False, download_name=f"Demand_{sale['invoice_code']}.pdf", mimetype='application/pdf')
@app.route('/generate_client_statement/<int:client_id>')
def generate_client_statement(client_id):
    sales = get_sales_query(" AND s.client_id = ? AND IFNULL(s.payment_status, '') NOT IN ('PAID', 'BAD DEBT', 'CANCELLED') AND UPPER(IFNULL(s.ownership_status, '')) != 'NOT OWNED'", [client_id])
    if not sales: return "No sales history found for this client", 404
    settings = get_settings()
    curr_label = settings.get('currency', 'UGX')
    pdf = DemandNotePDF(settings); pdf.add_page()
    pdf.set_font('helvetica', 'B', 16); pdf.cell(0, 10, 'CONSOLIDATED DEMAND NOTE', ln=1, align='C'); pdf.ln(5)
    pdf.set_font('helvetica', 'B', 11); pdf.cell(0, 7, f"Client: {sales[0]['client_name']}", ln=1); pdf.ln(5)
    
    pdf.set_fill_color(240, 240, 240); pdf.set_font('helvetica', 'B', 9)
    pdf.cell(30, 10, 'Invoice', 1, 0, 'C', True); pdf.cell(30, 10, 'Date', 1, 0, 'C', True); pdf.cell(90, 10, 'Description', 1, 0, 'L', True); pdf.cell(40, 10, f'Amount ({curr_label})', 1, 1, 'R', True)
    
    total = 0
    pdf.set_font('helvetica', '', 9)
    for s in sales:
        pdf.cell(30, 8, s['invoice_code'], 1); pdf.cell(30, 8, s['completion_date'], 1); pdf.cell(90, 8, s['contract_details'][:50], 1); pdf.cell(40, 8, f"{(s['total'] or 0):,.0f}", 1, 1, 'R')
        total += s['total']
    
    pdf.set_font('helvetica', 'B', 11); pdf.set_x(130); pdf.cell(30, 10, "TOTAL DUE:", 1, 0, 'R'); pdf.cell(40, 10, f"{total:,.0f}", 1, 1, 'R')
    
    out = io.BytesIO(); pdf.output(out); out.seek(0)
    return send_file(out, as_attachment=False, download_name="Demand_Note.pdf", mimetype='application/pdf')

@app.route('/generate_financial_report')
def generate_financial_report():
    client_id = request.args.get('client_id')
    area_id = request.args.get('area_id')
    status = request.args.get('status')
    
    where = ""
    params = []
    if client_id: where += " AND s.client_id = ?"; params.append(client_id)
    if area_id: where += " AND s.area_id = ?"; params.append(area_id)
    if status: where += " AND s.payment_status = ?"; params.append(status)
    
    sales = get_sales_query(where, params)
    active_sales = [s for s in sales if s.get('payment_status','').upper() not in ('BAD DEBT','CANCELLED') and s.get('ownership_status','').upper() != 'NOT OWNED']
    settings = get_settings()
    pdf = DemandNotePDF(settings); pdf.add_page()
    pdf.set_font('helvetica', 'B', 16); pdf.cell(0, 10, 'FINANCIAL SUMMARY REPORT', ln=1, align='C'); pdf.ln(5)
    
    # Headers
    pdf.set_fill_color(240, 240, 240); pdf.set_font('helvetica', 'B', 8)
    pdf.cell(20, 8, 'ID', 1, 0, 'C', True); pdf.cell(80, 8, 'Client / Description', 1, 0, 'L', True); pdf.cell(30, 8, 'Status', 1, 0, 'C', True); pdf.cell(30, 8, 'Amount', 1, 0, 'R', True); pdf.cell(30, 8, 'Profit', 1, 1, 'R', True)
    
    pdf.set_font('helvetica', '', 8)
    total_rev = 0; total_profit = 0
    for s in active_sales:
        pdf.cell(20, 7, s['entry_id'], 1)
        pdf.cell(80, 7, f"{s['client_name']} - {s['contract_details'][:30]}...", 1)
        pdf.cell(30, 7, s['payment_status'], 1, 0, 'C')
        pdf.cell(30, 7, f"{(s['total'] or 0):,.0f}", 1, 0, 'R')
        profit = s['profit']
        pdf.cell(30, 7, f"{profit:,.0f}", 1, 1, 'R')
        total_rev += s['total']; total_profit += profit
        
    pdf.set_font('helvetica', 'B', 10)
    pdf.cell(130, 10, "TOTALS:", 1, 0, 'R'); pdf.cell(30, 10, f"{total_rev:,.0f}", 1, 0, 'R'); pdf.cell(30, 10, f"{total_profit:,.0f}", 1, 1, 'R')
    
    out = io.BytesIO(); pdf.output(out); out.seek(0)
    return send_file(out, as_attachment=False, download_name="Financial_Report.pdf", mimetype='application/pdf')

# --- BROWSER-BASED PRINTABLE REPORTS ---

@app.route('/print_demand_note/<int:sale_id>')
def print_demand_note(sale_id):
    sale = get_sales_query(" AND s.id = ?", [sale_id])
    if not sale: return "Not found", 404
    sale = sale[0]
    is_ghost = sale.get('payment_status','').upper() == 'CANCELLED' or sale.get('ownership_status','').upper() == 'NOT OWNED'
    if is_ghost:
        return "Access Denied: Cannot generate a demand note for a ghost entry.", 403
    if sale.get('payment_status','').upper() == 'BAD DEBT':
        return "Access Denied: Cannot generate a demand note for a bad debt entry.", 403
    return render_template('report_print.html', 
                         report_type='demand_note',
                         title='Demand Note',
                         sale=sale,
                         reference=sale['invoice_code'],
                         settings=get_settings(),
                         now=datetime.datetime.now())
@app.route('/print_client_statement/<int:client_id>')
def print_client_statement(client_id):
    areas_param = request.args.get('areas')
    if areas_param:
        try:
            area_ids = []
            include_unassigned = False
            for x in areas_param.split(','):
                val = int(x)
                if val == 0:
                    include_unassigned = True
                else:
                    area_ids.append(val)
            
            where_clause = " AND s.client_id = ? AND IFNULL(s.payment_status, '') NOT IN ('PAID', 'BAD DEBT', 'CANCELLED') AND UPPER(IFNULL(s.ownership_status, '')) != 'NOT OWNED'"
            params = [client_id]
            
            if area_ids and include_unassigned:
                placeholders = ",".join(["?"] * len(area_ids))
                where_clause += f" AND (s.area_id IN ({placeholders}) OR s.area_id IS NULL OR s.area_id = 0)"
                params.extend(area_ids)
            elif area_ids:
                placeholders = ",".join(["?"] * len(area_ids))
                where_clause += f" AND s.area_id IN ({placeholders})"
                params.extend(area_ids)
            elif include_unassigned:
                where_clause += " AND (s.area_id IS NULL OR s.area_id = 0)"
                
            sales = get_sales_query(where_clause, params)
        except ValueError:
            sales = get_sales_query(" AND s.client_id = ? AND IFNULL(s.payment_status, '') NOT IN ('PAID', 'BAD DEBT', 'CANCELLED') AND UPPER(IFNULL(s.ownership_status, '')) != 'NOT OWNED'", [client_id])
    else:
        sales = get_sales_query(" AND s.client_id = ? AND IFNULL(s.payment_status, '') NOT IN ('PAID', 'BAD DEBT', 'CANCELLED') AND UPPER(IFNULL(s.ownership_status, '')) != 'NOT OWNED'", [client_id])

    if not sales: return "No unpaid items found for this client matching the selected areas", 200
    
    # Sort by date ascending (earliest first)
    sales.sort(key=lambda x: x['completion_date'] or '')
    
    conn = get_db()
    client = conn.execute("SELECT * FROM client_list WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    
    summary = {
        'revenue': sum(s['total'] for s in sales),
        'paid': sum(s['total'] for s in sales if s['payment_status'] == 'PAID'),
        'unpaid': sum(s['total'] for s in sales if s['payment_status'] != 'PAID')
    }
    
    return render_template('report_print.html', 
                         report_type='statement',
                         title='Demand Note',
                         client=client,
                         sales=sales,
                         summary=summary,
                         settings=get_settings(),
                         now=datetime.datetime.now())

@app.route('/api/client_unpaid_areas/<int:client_id>')
def client_unpaid_areas(client_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT s.area_id, a.name 
        FROM sales s
        LEFT JOIN area_list a ON s.area_id = a.id
        WHERE s.client_id = ? AND IFNULL(s.payment_status, '') NOT IN ('PAID', 'BAD DEBT', 'CANCELLED') AND UPPER(IFNULL(s.ownership_status, '')) != 'NOT OWNED'
    """, (client_id,)).fetchall()
    conn.close()
    return jsonify([
        {
            'id': r['area_id'] if r['area_id'] is not None else 0,
            'name': r['name'] if r['name'] is not None else 'Unassigned'
        } for r in rows
    ])

@app.route('/print_payments_report')
def print_payments_report():
    pay_start = request.args.get('pay_start')
    pay_end = request.args.get('pay_end')
    
    if pay_start is None and pay_end is None:
        # Default to current month on initial load
        today = datetime.date.today()
        pay_start = datetime.date(today.year, today.month, 1).strftime("%Y-%m-%d")
        if today.month == 12:
            pay_end = datetime.date(today.year, 12, 31).strftime("%Y-%m-%d")
        else:
            pay_end = (datetime.date(today.year, today.month + 1, 1) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    c_id = request.args.get('client_id')
    a_id = request.args.get('area_id')
    
    where = " AND s.payment_status = 'PAID'"
    params = []
    if c_id: where += " AND s.client_id = ?"; params.append(c_id)
    if a_id: where += " AND s.area_id = ?"; params.append(a_id)
    
    sales = get_sales_query(where, params)
    
    # Filter by date range
    filtered_sales = []
    for s in sales:
        p_date = s.get('payment_date') or s.get('completion_date') or ''
        if p_date:
            if pay_start and p_date < pay_start:
                continue
            if pay_end and p_date > pay_end:
                continue
            filtered_sales.append(s)
            
    filtered_sales.sort(key=lambda x: x.get('payment_date') or '', reverse=True)
    
    summary = {
        'revenue': sum(s['total'] for s in filtered_sales),
        'paid': sum(s['total'] for s in filtered_sales),
        'unpaid': 0,
        'profit': sum(s['profit'] for s in filtered_sales)
    }
    
    # Format range string for header
    range_str = f"{pay_start} to {pay_end}"
    try:
        d1 = datetime.datetime.strptime(pay_start, "%Y-%m-%d").strftime("%d %b %Y")
        d2 = datetime.datetime.strptime(pay_end, "%Y-%m-%d").strftime("%d %b %Y")
        range_str = f"{d1} - {d2}"
    except:
        pass
        
    title = f"Payments Report ({range_str})"
    
    return render_template('report_print.html', 
                         report_type='monthly_payments',
                         title=title,
                         sales=filtered_sales,
                         summary=summary,
                         settings=get_settings(),
                         now=datetime.datetime.now())

@app.route('/print_ura_report')
def print_ura_report():
    ura_filter = request.args.get('ura_filter', 'ALL').upper()
    sales = get_filtered_sales_from_request()
    ura_sales = [s for s in sales if s.get('is_vat_rated')]

    if ura_filter and ura_filter != 'ALL':
        if ura_filter == 'DUE':
            ura_sales = [s for s in ura_sales if (s.get('ura_status') or '').upper() == 'UNPAID' and s.get('payment_status') == 'PAID']
        elif ura_filter == 'NOT_DUE':
            ura_sales = [s for s in ura_sales if (s.get('ura_status') or '').upper() == 'UNPAID' and s.get('payment_status') != 'PAID']
        else:
            ura_sales = [s for s in ura_sales if (s.get('ura_status') or '').upper() == ura_filter]

    ura_sales = sorted(ura_sales, key=lambda x: x.get('tax_invoice_date') or x.get('completion_date') or '', reverse=True)

    # Calculate is_high_risk for each URA sale (unpaid and due for over 30 days)
    today = datetime.date.today()
    for s in ura_sales:
        is_high_risk = False
        if s.get('payment_status') == 'PAID' and s.get('ura_status') == 'UNPAID':
            pay_date_str = s.get('payment_date') or s.get('completion_date') or ''
            if pay_date_str:
                try:
                    pay_date = datetime.datetime.strptime(pay_date_str.split(' ')[0], "%Y-%m-%d").date()
                    days_due = (today - pay_date).days
                    if days_due > 30:
                        is_high_risk = True
                except ValueError:
                    pass
        s['is_high_risk'] = is_high_risk

    ura_summary = {
        'total_vat': sum((s.get('vat') or 0) for s in ura_sales),
        'unpaid_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'UNPAID'),
        'paid_vat': sum((s.get('vat') or 0) for s in ura_sales if s.get('ura_status') == 'PAID'),
        'unpaid_count': sum(1 for s in ura_sales if s.get('ura_status') == 'UNPAID'),
        'paid_count': sum(1 for s in ura_sales if s.get('ura_status') == 'PAID'),
    }

    filter_label = ura_filter.title() if ura_filter != 'ALL' else 'All Statuses'
    title = f"URA Tax Report — {filter_label}"

    return render_template('report_print.html',
                           report_type='ura_report',
                           title=title,
                           sales=ura_sales,
                           ura_summary=ura_summary,
                           ura_filter=ura_filter,
                           summary={'revenue': sum(s['total'] for s in ura_sales)},
                           settings=get_settings(),
                           now=datetime.datetime.now())

@app.route('/api/export_payments_excel')
def export_payments_excel():
    pay_start = request.args.get('pay_start')
    pay_end = request.args.get('pay_end')
    c_id = request.args.get('client_id')
    a_id = request.args.get('area_id')
    
    where = " AND s.payment_status = 'PAID'"
    params = []
    if c_id: where += " AND s.client_id = ?"; params.append(c_id)
    if a_id: where += " AND s.area_id = ?"; params.append(a_id)
    
    sales = get_sales_query(where, params)
    
    # Filter by date range
    filtered_sales = []
    for s in sales:
        p_date = s.get('payment_date') or s.get('completion_date') or ''
        if p_date:
            if pay_start and p_date < pay_start:
                continue
            if pay_end and p_date > pay_end:
                continue
            filtered_sales.append(s)
            
    filtered_sales.sort(key=lambda x: x.get('payment_date') or '', reverse=True)
    
    settings = get_settings()
    curr_label = settings.get('currency', 'UGX')
    cols = {
        'entry_id': 'Entry ID',
        'payment_date': 'Payment Date',
        'client_name': 'Client Name',
        'invoice_code': 'Invoice No',
        'contract_details': 'Contract Details',
        'area_name': 'Supply Area',
        'total': f'Amount ({curr_label})',
        'profit': f'Estimated Profit ({curr_label})'
    }
    
    import io
    import pandas as pd
    if not filtered_sales:
        df = pd.DataFrame(columns=list(cols.keys()))
    else:
        df = pd.DataFrame(filtered_sales)
        for col in cols.keys():
            if col not in df.columns:
                df[col] = ''
        df = df[list(cols.keys())]
    df = df.rename(columns=cols)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Payments')
    output.seek(0)
    
    filename = f"Payments_Report_{pay_start}_to_{pay_end}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route('/print_financial_report')
def print_financial_report():
    sales = get_filtered_sales_from_request()
    # Sort by date ascending (earliest first)
    sales.sort(key=lambda x: x['completion_date'] or '')
    
    active_sales = [s for s in sales if s.get('payment_status','').upper() not in ('BAD DEBT','CANCELLED') and s.get('ownership_status','').upper() != 'NOT OWNED']
    summary = {
        'revenue': sum(s['total'] for s in active_sales),
        'investment': sum((s['investment_amount'] or 0) for s in active_sales),
        'profit': sum(s['profit'] for s in active_sales)
    }
    
    return render_template('report_print.html', 
                         report_type='financial',
                         title='Financial Summary',
                         sales=active_sales,
                         summary=summary,
                         settings=get_settings(),
                         now=datetime.datetime.now())

@app.route('/api/client_snapshot/<int:client_id>')
def client_snapshot(client_id):
    sales = get_sales_query(" AND s.client_id = ?", [client_id])
    if not sales: return jsonify({'error': 'No data'}), 404
    
    conn = get_db(); conn.row_factory = sqlite3.Row
    client = conn.execute("SELECT * FROM client_list WHERE id = ?", (client_id,)).fetchone(); conn.close()
    
    active_sales = [s for s in sales if s.get('payment_status','').upper() not in ('BAD DEBT','CANCELLED') and s.get('ownership_status','').upper() != 'NOT OWNED']
    stats = {
        'name': client['name'],
        'count': len(active_sales),
        'total_rev': sum(s['total'] for s in active_sales),
        'total_profit': sum(s['profit'] for s in active_sales),
        'unpaid': sum(s['total'] for s in active_sales if s['payment_status'] != 'PAID'),
        'avg_value': sum(s['total'] for s in active_sales) / len(active_sales) if active_sales else 0,
        'recent': sales
    }
    return jsonify(stats)

@app.route('/api/area_snapshot/<int:area_id>')
def area_snapshot(area_id):
    sales = get_sales_query(" AND s.area_id = ?", [area_id])
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    area = conn.execute("SELECT * FROM area_list WHERE id = ?", (area_id,)).fetchone()
    conn.close()
    
    if not area: return jsonify({'error': 'Area not found'}), 404
    
    active_sales = [s for s in sales if s.get('payment_status','').upper() not in ('BAD DEBT','CANCELLED') and s.get('ownership_status','').upper() != 'NOT OWNED']
    stats = {
        'name': area['name'],
        'count': len(active_sales),
        'total_rev': sum(s['total'] for s in active_sales),
        'total_profit': sum(s['profit'] for s in active_sales),
        'unpaid': sum(s['total'] for s in active_sales if s['payment_status'] != 'PAID'),
        'avg_value': sum(s['total'] for s in active_sales) / len(active_sales) if active_sales else 0,
        'recent': sales
    }
    return jsonify(stats)

@app.route('/api/bulk_edit', methods=['POST'])
def bulk_edit():
    data = request.json
    ids = data.get('ids', [])
    action = data.get('action')
    value = data.get('value')
    
    if not ids or not action:
        return jsonify({'error': 'IDs and action required'}), 400
        
    conn = get_db()
    cur = conn.cursor()
    
    try:
        if action == 'payment_status':
            cur.execute(f"UPDATE sales SET payment_status = ? WHERE id IN ({','.join(['?']*len(ids))})", [value] + ids)
        elif action == 'ura_status':
            cur.execute(f"UPDATE sales SET ura_status = ? WHERE id IN ({','.join(['?']*len(ids))})", [value] + ids)
        elif action == 'delete':
            cur.execute(f"DELETE FROM sales WHERE id IN ({','.join(['?']*len(ids))})", ids)
        elif action == 'payment_date':
            cur.execute(f"UPDATE sales SET payment_date = ? WHERE id IN ({','.join(['?']*len(ids))})", [value] + ids)
            
        conn.commit()
        return jsonify({'success': True, 'count': len(ids)})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/add_client_fast', methods=['POST'])
def add_client_fast():
    name = request.form.get('name')
    if not name: return jsonify({'error': 'Name required'}), 400
    conn = get_db(); cur = conn.cursor()
    code = f"CL-{random.randint(100, 999)}"
    cur.execute("INSERT INTO client_list (name, code) VALUES (?, ?)", (name, code))
    new_id = cur.lastrowid; conn.commit(); conn.close()
    return jsonify({'id': new_id, 'name': name})

@app.route('/api/add_area_fast', methods=['POST'])
def add_area_fast():
    name = (request.form.get('name') or '').strip()
    if not name: return jsonify({'error': 'Name required'}), 400
    conn = get_db()
    existing = conn.execute("SELECT id FROM area_list WHERE LOWER(name) = ? AND delete_flag = 0", (name.lower(),)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': f"A supply area with the name '{name}' already exists (case-insensitive)."}), 400
    cur = conn.cursor()
    cur.execute("INSERT INTO area_list (name) VALUES (?)", (name,))
    new_id = cur.lastrowid; conn.commit(); conn.close()
    return jsonify({'id': new_id, 'name': name})

@app.route('/api/import_excel', methods=['POST'])
def import_excel():
    if 'file' not in request.files: return redirect(url_for('demands'))
    file = request.files['file']
    if not file.filename: return redirect(url_for('demands'))
    
    import pandas as pd
    import numpy as np
    try:
        df = pd.read_excel(file)
        conn = get_db()
        conn.row_factory = sqlite3.Row
        
        # Helper to find column regardless of case or spaces
        def find_col(possible_names):
            for col in df.columns:
                c_clean = str(col).lower().replace(' ', '').replace('_', '').replace('-', '')
                for p in possible_names:
                    p_clean = p.lower().replace(' ', '').replace('_', '').replace('-', '')
                    if c_clean == p_clean: return col
            return None

        col_map = {
            'invoice': find_col(['Invoice', 'InvoiceNo', 'InvoiceCode']),
            'contract': find_col(['Contract', 'ContractNo', 'ContractNumber']),
            'po': find_col(['PO', 'PONo', 'PONumber', 'PurchaseOrder']),
            'client': find_col(['Client', 'ClientName', 'Customer', 'CustomerName']),
            'area': find_col(['Area', 'AreaName', 'Location', 'Region']),
            'details': find_col(['Details', 'Description', 'ContractDetails', 'Particulars']),
            'date': find_col(['Date', 'CompletionDate', 'DeliveryDate', 'SaleDate']),
            'total': find_col(['Amount', 'Total', 'TotalAmount', 'Value', 'Price']),
            'investment': find_col(['Investment', 'Cost', 'InvestmentAmount', 'PurchasePrice']),
            'type': find_col(['Type', 'ContractType', 'Category']),
            'ownership': find_col(['Ownership', 'OwnershipStatus']),
            'status': find_col(['Status', 'PaymentStatus', 'Payment']),
            'ura': find_col(['URA', 'URAStatus', 'FilingStatus']),
            'company': find_col(['Company', 'CompanyName', 'SupplyingCompany'])
        }

        # Helper to get safe value from row
        def get_row_val(row, map_key, default=''):
            col_name = col_map.get(map_key)
            if col_name and col_name in row:
                val = row[col_name]
                if pd.isna(val): return default
                return str(val).strip()
            return default

        def get_row_float(row, map_key):
            val = get_row_val(row, map_key, '0')
            try:
                # Remove commas or currency symbols if any
                clean_val = str(val).replace(',', '').replace('UGX', '').strip()
                return float(clean_val)
            except: return 0.0

        def get_row_float_or_none(row, map_key):
            val = get_row_val(row, map_key, '')
            if not val or val.lower() == 'nan':
                return None
            try:
                # Remove commas or currency symbols if any
                clean_val = str(val).replace(',', '').replace('UGX', '').strip()
                return float(clean_val)
            except: return None

        # Get last entry ID to increment
        last_sale = conn.execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
        last_id_num = last_sale[0] if last_sale else 0

        count = 0
        settings = get_settings()
        for _, row in df.iterrows():
            # Client extraction
            cName = get_row_val(row, 'client')
            if not cName or cName.lower() == 'nan': continue
            
            # Check if client exists
            c = conn.execute("SELECT id FROM client_list WHERE name = ?", (cName,)).fetchone()
            cid = c['id'] if c else None
            if not cid:
                cur = conn.cursor()
                cur.execute("INSERT INTO client_list (name, code) VALUES (?, ?)", (cName, f"CL-{random.randint(100, 999)}"))
                cid = cur.lastrowid
            
            # Area extraction
            aName = get_row_val(row, 'area')
            aid = None
            if aName and aName.lower() != 'nan':
                a = conn.execute("SELECT id FROM area_list WHERE name = ?", (aName,)).fetchone()
                aid = a['id'] if a else None
                if not aid:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO area_list (name) VALUES (?)", (aName,))
                    aid = cur.lastrowid

            last_id_num += 1
            new_entry_id = f"{last_id_num:03d}"

            conn.execute("""
                INSERT INTO sales (entry_id, invoice_code, contract_no, po_no, client_id, area_id, completion_date, contract_details, total, investment_amount, payment_status, ownership_status, is_gov, is_vat_rated, contract_type, ura_status, company_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                new_entry_id, 
                get_row_val(row, 'invoice'),
                get_row_val(row, 'contract'),
                get_row_val(row, 'po'),
                cid, aid,
                get_row_val(row, 'date'),
                get_row_val(row, 'details'),
                get_row_float(row, 'total'),
                get_row_float_or_none(row, 'investment'),
                get_row_val(row, 'status', 'NOT PAID'),
                get_row_val(row, 'ownership', 'Owned'),
                0, 1,
                get_row_val(row, 'type', 'Supply'),
                get_row_val(row, 'ura', 'Not Filed'),
                get_row_val(row, 'company', settings.get('name', ''))
            ))
            count += 1
        
        conn.commit(); conn.close()
        if count > 0:
            flash(f"Success: {count} records imported into the Registry.")
        else:
            flash("Warning: No valid records found in the Excel file. Please check your column names.")
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        flash(f"Import failed: {str(e)}")
    
    return redirect(url_for('demands'))

@app.route('/clients')
def clients():
    conn = get_db()
    # Fetch clients and their total revenue for sorting, though we don't display it per user request
    lst = conn.execute("""
        SELECT c.*, IFNULL(SUM(CASE WHEN IFNULL(s.payment_status, '') IN ('BAD DEBT', 'CANCELLED') THEN 0 ELSE s.total END), 0) as total_revenue 
        FROM client_list c 
        LEFT JOIN sales s ON c.id = s.client_id 
        WHERE c.delete_flag = 0 
        GROUP BY c.id 
        ORDER BY total_revenue DESC
    """).fetchall()
    conn.close()
    return render_template('clients.html', active_page='clients', clients=lst)

@app.route('/clients/add', methods=['POST'])
def add_client():
    conn = get_db(); conn.execute("INSERT INTO client_list (code, name, tin, address) VALUES (?,?,?,?)", (f"CL-{random.randint(1000,9999)}", request.form.get('name'), request.form.get('tin'), request.form.get('address'))); conn.commit(); conn.close()
    return redirect(url_for('clients'))

@app.route('/clients/update', methods=['POST'])
def update_client():
    f = request.form; conn = get_db()
    conn.execute("UPDATE client_list SET name=?, tin=?, address=? WHERE id=?", (f.get('name'), f.get('tin'), f.get('address'), f.get('id')))
    conn.commit(); conn.close(); return redirect(url_for('clients'))

@app.route('/areas')
def areas():
    conn = get_db()
    lst = conn.execute("""
        SELECT a.*, COUNT(s.id) as contract_count, IFNULL(SUM(CASE WHEN IFNULL(s.payment_status, '') IN ('BAD DEBT', 'CANCELLED') THEN 0 ELSE s.total END), 0) as total_value
        FROM area_list a
        LEFT JOIN sales s ON a.id = s.area_id
        WHERE a.delete_flag = 0
        GROUP BY a.id
        ORDER BY a.name COLLATE NOCASE ASC
    """).fetchall()
    conn.close()
    return render_template('areas.html', active_page='areas', areas=lst)

@app.route('/api/supply_areas')
def api_supply_areas():
    conn = get_db()
    lst = conn.execute("""
        SELECT a.*, COUNT(s.id) as contract_count, IFNULL(SUM(CASE WHEN IFNULL(s.payment_status, '') IN ('BAD DEBT', 'CANCELLED') THEN 0 ELSE s.total END), 0) as total_value
        FROM area_list a
        LEFT JOIN sales s ON a.id = s.area_id
        WHERE a.delete_flag = 0
        GROUP BY a.id
        ORDER BY a.name COLLATE NOCASE ASC
    """).fetchall()
    conn.close()
    return jsonify([
        {
            'id': r['id'],
            'name': r['name'],
            'contract_count': r['contract_count'],
            'total_value': r['total_value']
        } for r in lst
    ])

@app.route('/api/supply_areas/update', methods=['POST'])
def api_update_area():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized access.'}), 403
    
    if request.is_json:
        data = request.json
        area_id = data.get('id')
        name = data.get('name')
    else:
        area_id = request.form.get('id')
        name = request.form.get('name')
        
    if not area_id or not name:
        return jsonify({'error': 'ID and Name are required.'}), 400
        
    name = name.strip()
    conn = get_db()
    existing = conn.execute("SELECT id FROM area_list WHERE LOWER(name) = ? AND id != ? AND delete_flag = 0", (name.lower(), area_id)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': f"A supply area with the name '{name}' already exists (case-insensitive)."}), 400
        
    conn.execute("UPDATE area_list SET name=? WHERE id=?", (name, area_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/areas/add', methods=['POST'])
def add_area():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash("Supply Area name cannot be empty.", "error")
        return redirect(url_for('areas'))
        
    conn = get_db()
    existing = conn.execute("SELECT id FROM area_list WHERE LOWER(name) = ? AND delete_flag = 0", (name.lower(),)).fetchone()
    if existing:
        conn.close()
        flash(f"Error: A supply area with the name '{name}' already exists (case-insensitive).", "error")
        return redirect(url_for('areas'))
        
    conn.execute("INSERT INTO area_list (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    return redirect(url_for('areas'))

@app.route('/areas/update', methods=['POST'])
def update_area():
    f = request.form
    area_id = f.get('id')
    name = (f.get('name') or '').strip()
    if not area_id or not name:
        flash("Area ID and Name are required.", "error")
        return redirect(url_for('areas'))
        
    conn = get_db()
    existing = conn.execute("SELECT id FROM area_list WHERE LOWER(name) = ? AND id != ? AND delete_flag = 0", (name.lower(), area_id)).fetchone()
    if existing:
        conn.close()
        flash(f"Error: A supply area with the name '{name}' already exists (case-insensitive).", "error")
        return redirect(url_for('areas'))
        
    conn.execute("UPDATE area_list SET name=? WHERE id=?", (name, area_id))
    conn.commit()
    conn.close()
    return redirect(url_for('areas'))

@app.route('/settings')
def settings():
    if not session.get('user_id'):
        return redirect(url_for('google_login_page'))
    return render_template('settings.html', active_page='settings', settings=get_settings())

def migrate_assets():
    """Ensure all assets follow the 'current_asset' naming convention"""
    conn = get_db()
    try:
        settings = conn.execute("SELECT * FROM company_settings WHERE id = 1").fetchone()
        if not settings:
            return
            
        updated = {}
        for key in ['logo', 'header', 'footer', 'icon']:
            old_path = settings[f'{key}_path']
            if old_path and not old_path.startswith('current_'):
                ext = os.path.splitext(old_path)[1].lower()
                new_name = f"current_{key}{ext}"
                old_full = os.path.join(UPLOAD_FOLDER, old_path)
                new_full = os.path.join(UPLOAD_FOLDER, new_name)
                
                if os.path.exists(old_full):
                    for ex in ['.png', '.jpg', '.jpeg', '.gif', '.icns', '.ico']:
                        existing = os.path.join(UPLOAD_FOLDER, f"current_{key}{ex}")
                        if os.path.exists(existing) and existing != old_full:
                            try: os.remove(existing)
                            except: pass
                    
                    try:
                        os.rename(old_full, new_full)
                        updated[f'{key}_path'] = new_name
                    except Exception as e:
                        print(f"Failed to rename {old_path}: {e}")
                else:
                    if os.path.exists(new_full):
                        updated[f'{key}_path'] = new_name
        
        if updated:
            set_clause = ", ".join([f"{k} = ?" for k in updated.keys()])
            conn.execute(f"UPDATE company_settings SET {set_clause} WHERE id = 1", list(updated.values()))
            conn.commit()
    except Exception as e:
        print(f"Asset migration failed: {e}")
    finally:
        conn.close()

def sync_launcher_icon():
    """Update the desktop launcher and its icon based on current settings"""
    try:
        # Only run launcher sync when explicitly allowed. This prevents the app
        # from changing the desktop/application icon automatically on launch.
        # To enable, set environment variable ALLOW_LAUNCHER_SYNC=1 before running.
        if os.environ.get('ALLOW_LAUNCHER_SYNC', '0') != '1':
            print('Launcher sync disabled (set ALLOW_LAUNCHER_SYNC=1 to enable)')
            return

        script_dir = get_resource_path("")
        create_script = os.path.join(script_dir, "create_launcher.sh")
        change_script = os.path.join(script_dir, "change_icon.sh")
        
        import subprocess
        if os.path.exists(create_script):
            print(f"Running {create_script}...")
            subprocess.run(["bash", create_script], check=False, cwd=script_dir)
        
        conn = get_db()
        try:
            res = conn.execute("SELECT icon_path FROM company_settings WHERE id = 1").fetchone()
            if res and res['icon_path']:
                icon_full_path = os.path.join(UPLOAD_FOLDER, res['icon_path'])
                if os.path.exists(icon_full_path):
                    print(f"Preparing AppIcon.icns from {icon_full_path} for builds...")
                    # Create AppIcon.icns in project root so build scripts will pick it up
                    iconset_dir = os.path.join(script_dir, 'app_icon.iconset')
                    try:
                        os.makedirs(iconset_dir, exist_ok=True)
                        for sz in (16,32,128,256,512,1024):
                            outp = os.path.join(iconset_dir, f'icon_{sz}x{sz}.png')
                            outp2 = os.path.join(iconset_dir, f'icon_{sz}x{sz}@2x.png')
                            subprocess.run(['sips','-s','format','png','-z',str(sz),str(sz),icon_full_path,'--out',outp], check=False)
                            subprocess.run(['sips','-s','format','png','-z',str(sz*2),str(sz*2),icon_full_path,'--out',outp2], check=False)
                        icns_out = os.path.join(script_dir, 'AppIcon.icns')
                        subprocess.run(['iconutil','-c','icns',iconset_dir,'-o',icns_out], check=False)
                    finally:
                        try: shutil.rmtree(iconset_dir)
                        except: pass
                    # Optionally update desktop launcher (only if allowed)
                    if os.path.exists(change_script) and os.environ.get('ALLOW_LAUNCHER_SYNC','0') == '1':
                        print(f"Applying custom icon runtime using {change_script}...")
                        subprocess.run(["bash", change_script, icon_full_path], check=False, cwd=script_dir)
        finally:
            conn.close()
        
        print("✅ Launcher synchronization complete.")
    except Exception as e:
        print(f"⚠️ Launcher sync failed: {e}")

@app.route('/settings/update', methods=['POST'])
def update_settings():
    f = request.form; conn = get_db()
    
    # Financial fields with safe parsing
    try: vat_rate = float(f.get('vat_rate', 18.0))
    except (ValueError, TypeError): vat_rate = 18.0
    
    try: wht_rate = float(f.get('wht_rate', 6.0))
    except (ValueError, TypeError): wht_rate = 6.0

    try: vwht_rate = float(f.get('vwht_rate', 6.0))
    except (ValueError, TypeError): vwht_rate = 6.0
    
    try: profit_margin = float(f.get('profit_margin', 50.0))
    except (ValueError, TypeError): profit_margin = 50.0

    currency = (f.get('currency') or 'UGX').strip().upper()

    conn.execute("""
        UPDATE company_settings 
        SET name=?, address=?, contact=?, email=?, website=?, reg_number=?,
            bank_name=?, bank_account_name=?, bank_account_number=?, bank_branch=?, 
            tin_number=?, footer=?, vat_rate=?, wht_rate=?, vwht_rate=?, profit_margin=?, currency=?
        WHERE id=1
    """, (
        f.get('name', '').strip(), f.get('address', '').strip(), f.get('contact', '').strip(), f.get('email', '').strip(), f.get('website', '').strip(), f.get('reg_number', '').strip(),
        f.get('bank_name', '').strip(), f.get('bank_account_name', '').strip(), f.get('bank_account_number', '').strip(), f.get('bank_branch', '').strip(), 
        f.get('tin_number', '').strip(), f.get('footer', '').strip(), vat_rate, wht_rate, vwht_rate, profit_margin, currency
    ))
    
    # Invalidate request cache if present
    if hasattr(g, '_cached_company_settings'):
        delattr(g, '_cached_company_settings')
    
    # Handle user profile picture upload
    profile_file = request.files.get('profile_picture')
    if profile_file and profile_file.filename and session.get('user_id'):
        profile_content = profile_file.read()
        conn.execute("UPDATE users SET profile_picture_blob = ? WHERE id = ?", (sqlite3.Binary(profile_content), session.get('user_id')))

    # Handle branding image assets (logo, header, footer)
    for key in ['logo', 'header', 'footer']:
        file = request.files.get(key)
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            fname = f"current_{key}{ext}"
            full_path = os.path.join(UPLOAD_FOLDER, fname)
            
            file_content = file.read()
            file.seek(0)
            
            for ex in ['.png', '.jpg', '.jpeg', '.gif', '.icns', '.ico']:
                old_file = os.path.join(UPLOAD_FOLDER, f"current_{key}{ex}")
                if os.path.exists(old_file):
                    try: os.remove(old_file)
                    except: pass
            
            file.save(full_path)
            conn.execute(f"UPDATE company_settings SET {key}_path = ?, {key}_blob = ? WHERE id=1", (fname, sqlite3.Binary(file_content)))

    conn.commit(); conn.close()
    flash("Settings updated successfully.")
    return redirect(url_for('settings'))

@app.route('/settings/export')
def export_mdb():
    """ Export the current database as a portable .mdb file """
    settings = get_settings()
    filename = f"{settings.get('name', 'Company_Data').replace(' ', '_')}.mdb"
    return send_file(get_db_path(), as_attachment=True, download_name=filename)

@app.route('/settings/save')
def save_current():
    """ Manual save trigger (commits all changes) """
    flash("Changes saved to current file.")
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/settings/create_launcher')
def manual_create_launcher():
    if GCS_BUCKET_NAME:
        flash("Launcher creation is not available in cloud deployment.")
    else:
        sync_launcher_icon()
        flash("Desktop launcher created successfully.")
    return redirect(url_for('settings'))

@app.route('/sales/edit/<int:id>')
def edit_sale(id):
    conn = get_db(); sale = conn.execute("SELECT * FROM sales WHERE id = ?", (id,)).fetchone()
    cs = conn.execute("SELECT * FROM client_list WHERE delete_flag = 0 AND user_id = ?", (get_user_filter(),)).fetchall()
    as_ = conn.execute("SELECT * FROM area_list WHERE delete_flag = 0 AND user_id = ? ORDER BY name COLLATE NOCASE", (get_user_filter(),)).fetchall(); conn.close()
    settings = get_settings()
    return render_template('sales.html', active_page='demands', clients=cs, areas=as_, sale=sale, is_edit=True, settings=settings)

@app.route('/sales/update', methods=['POST'])
def update_sale():
    f = request.form
    try:
        conn = get_db()
        conn.execute('''UPDATE sales SET invoice_code=?, contract_no=?, po_no=?, client_id=?, area_id=?, completion_date=?, contract_details=?, company_name=?, contract_type=?, is_gov=?, is_vat_rated=?, payment_status=?, payment_date=?, ura_status=?, ownership_status=?, total=?, investment_amount=?, tax_invoice_date=?, tax_period=? WHERE id = ?''',
            (f.get('invoice'), f.get('contract_no'), f.get('po_no'), f.get('client_id'), f.get('area_id'), f.get('completion_date'), f.get('contract_details'), f.get('company_name'), f.get('contract_type'), int(f.get('is_gov') or 0), int(f.get('is_vat') or 0), f.get('payment_status'), f.get('payment_date'), f.get('ura_status'), f.get('ownership_status', 'Owned'), safe_float(f.get('amount')), None if (f.get('investment') is None or str(f.get('investment')).strip() == '') else safe_float(f.get('investment')), f.get('tax_invoice_date') or None, f.get('tax_period') if f.get('ura_status') in ['PAID', 'OFFSET'] else None, f.get('id')))
        conn.commit(); conn.close()
        if f.get('ajax') == '1':
            return jsonify({"success": True})
        flash("Updated."); return redirect(f.get('current_url', url_for('demands')))
    except Exception as e:
        if f.get('ajax') == '1':
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f"Error updating: {e}")
        return redirect(url_for('demands'))

@app.route('/sales/delete/<int:id>')
def delete_sale(id):
    conn = get_db(); conn.execute("DELETE FROM sales WHERE id = ?", (id,)); conn.commit(); conn.close()
    flash("Record deleted."); return redirect(request.referrer or url_for('all_entries'))

@app.route('/sales/bulk_action', methods=['POST'])
def bulk_action():
    action = request.form.get('action')
    ids = request.form.getlist('sale_ids')
    if not ids:
        flash("No items selected.")
        return redirect(url_for('demands'))
    
    conn = get_db()
    if action == 'delete':
        conn.execute(f"DELETE FROM sales WHERE id IN ({','.join(['?']*len(ids))})", ids)
        flash(f"Successfully deleted {len(ids)} records.")
    elif action == 'mark_paid':
        conn.execute(f"UPDATE sales SET payment_status = 'PAID', payment_date = date('now') WHERE id IN ({','.join(['?']*len(ids))})", ids)
        flash(f"Successfully marked {len(ids)} records as PAID.")
    elif action == 'mark_unpaid':
        conn.execute(f"UPDATE sales SET payment_status = 'NOT PAID' WHERE id IN ({','.join(['?']*len(ids))})", ids)
        flash(f"Successfully marked {len(ids)} records as NOT PAID.")
    
    conn.commit(); conn.close()
    return redirect(url_for('demands'))

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    if not q:
        # No query — return ALL records sorted by newest first
        res = conn.execute("""
            SELECT s.*, c.name as client_name, a.name as area_name
            FROM sales s
            LEFT JOIN client_list c ON s.client_id = c.id
            LEFT JOIN area_list a ON s.area_id = a.id
            ORDER BY s.id DESC
        """).fetchall()
    else:
        # Split into individual words and require each word matches at least one field (AND logic)
        words = q.split()
        base_query = """
            SELECT s.*, c.name as client_name, a.name as area_name
            FROM sales s
            LEFT JOIN client_list c ON s.client_id = c.id
            LEFT JOIN area_list a ON s.area_id = a.id
            WHERE
        """
        conditions = []
        params = []
        for word in words:
            w = f"%{word}%"
            conditions.append("""(
                s.invoice_code LIKE ? OR s.contract_no LIKE ? OR s.po_no LIKE ?
                OR c.name LIKE ? OR s.entry_id LIKE ? OR s.contract_details LIKE ?
                OR a.name LIKE ? OR s.company_name LIKE ? OR s.payment_status LIKE ?
                OR s.contract_type LIKE ? OR s.ura_status LIKE ?
            )""")
            params.extend([w, w, w, w, w, w, w, w, w, w, w])
        full_query = base_query + " AND ".join(conditions) + " ORDER BY s.id DESC"
        res = conn.execute(full_query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in res])

@app.route('/api/sale/<int:id>')
def api_sale(id):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    res = conn.execute("SELECT * FROM sales WHERE id = ?", (id,)).fetchone()
    conn.close()
    return jsonify(dict(res) if res else {})

@app.route('/clients/delete/<int:id>')
def delete_client(id):
    conn = get_db(); conn.execute("UPDATE client_list SET delete_flag = 1 WHERE id = ?", (id,)); conn.commit(); conn.close(); return redirect(url_for('clients'))


@app.route('/api/export_excel')
def export_excel():
    sales = get_filtered_sales_from_request()
    settings = get_settings()
    curr = settings.get('currency', 'UGX')
    cols = {
        'entry_id': 'Entry ID',
        'completion_date': 'Date',
        'client_name': 'Client',
        'invoice_code': 'Invoice',
        'contract_details': 'Details',
        'area_name': 'Area',
        'total': f'Amount ({curr})',
        'payment_status': 'Status',
        'profit': f'Profit ({curr})'
    }
    if not sales:
        df = pd.DataFrame(columns=list(cols.keys()))
    else:
        df = pd.DataFrame(sales)
        df = df[list(cols.keys())]
    df = df.rename(columns=cols)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Report')
    output.seek(0)
    return send_file(output, as_attachment=True, download_name="Report_Export.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route('/api/live_stats')
def live_stats():
    conn = get_db()
    settings = get_settings(conn)
    fin = get_financial_sql_snippets(settings)
    stats = {
        'revenue': conn.execute("SELECT SUM(total) FROM sales WHERE NOT (UPPER(IFNULL(payment_status,'')) IN ('CANCELLED','BAD DEBT') OR UPPER(IFNULL(ownership_status,'')) = 'NOT OWNED')").fetchone()[0] or 0,
        'unpaid': conn.execute("SELECT SUM(total) FROM sales WHERE payment_status = 'NOT PAID' AND NOT (UPPER(IFNULL(ownership_status, '')) = 'NOT OWNED' OR UPPER(IFNULL(payment_status, '')) = 'CANCELLED')").fetchone()[0] or 0,
        'count': conn.execute("SELECT COUNT(*) FROM sales WHERE payment_status = 'NOT PAID' AND NOT (UPPER(IFNULL(ownership_status, '')) = 'NOT OWNED' OR UPPER(IFNULL(payment_status, '')) = 'CANCELLED')").fetchone()[0]
    }
    stats['profit'] = conn.execute(f"""
        SELECT SUM(profit) FROM (
            SELECT 
                {fin['profit_expr']} as profit
            FROM (
                SELECT total, investment_amount,
                       {fin['vat_expr']} as vat,
                       {fin['wht_expr']} as wht
                FROM sales s
                WHERE NOT (UPPER(IFNULL(payment_status,'')) IN ('CANCELLED','BAD DEBT') OR UPPER(IFNULL(ownership_status,'')) = 'NOT OWNED')
            )
        )
    """).fetchone()[0] or 0
    conn.close()
    return jsonify(stats)

def init_db():
    db_path = get_db_path()
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        import threading
        threading.Thread(target=backup_database, args=(db_path,), daemon=True).start()

    conn = get_db(); cursor = conn.cursor()

    # Set WAL mode and performance PRAGMAs once at startup (these settings persist in the DB file)
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA wal_autocheckpoint = 1000")
        cursor.execute("PRAGMA cache_size = -8000")   # 8MB page cache
        cursor.execute("PRAGMA temp_store = MEMORY")  # temp tables in RAM
    except Exception:
        pass

    # Check if this is a brand new database (i.e. system_info or users table doesn't exist yet, or is empty)
    is_new_db = False
    try:
        res_tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_info'").fetchone()
        if not res_tables:
            is_new_db = True
        else:
            row_count = cursor.execute("SELECT COUNT(*) FROM system_info").fetchone()[0]
            if row_count == 0:
                is_new_db = True
    except Exception:
        is_new_db = True

    cursor.execute('CREATE TABLE IF NOT EXISTS system_info (meta_field TEXT UNIQUE, meta_value TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS client_list (id INTEGER PRIMARY KEY, code TEXT, name TEXT, contact TEXT, tin TEXT, address TEXT, delete_flag INT DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS area_list (id INTEGER PRIMARY KEY, name TEXT, delete_flag INT DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY, entry_id TEXT, invoice_code TEXT, contract_no TEXT, po_no TEXT, client_id INT, area_id INT, completion_date TEXT, contract_details TEXT, company_name TEXT, contract_type TEXT, is_gov INT, is_vat_rated INT, payment_status TEXT, payment_date TEXT, ura_status TEXT, ownership_status TEXT, total REAL, investment_amount REAL, tax_invoice_date TEXT, tax_period TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (client_id) REFERENCES client_list(id) ON DELETE RESTRICT, FOREIGN KEY (area_id) REFERENCES area_list(id) ON DELETE RESTRICT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS company_settings (id INTEGER PRIMARY KEY, name TEXT, address TEXT, contact TEXT, email TEXT, bank_name TEXT, bank_account_name TEXT, bank_account_number TEXT, bank_branch TEXT, logo_path TEXT, header_path TEXT, footer_path TEXT, icon_path TEXT, logo_blob BLOB, header_blob BLOB, footer_blob BLOB, icon_blob BLOB, tin_number TEXT, footer TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, username TEXT, profile_picture_blob BLOB, is_admin INTEGER DEFAULT 0, password TEXT, biometrics_enabled INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
    
    # Check for missing columns (upgrade support)
    cursor.execute("PRAGMA table_info(company_settings)")
    cols = [c[1] for c in cursor.fetchall()]
    for b_col in ['logo_blob', 'header_blob', 'footer_blob', 'icon_blob']:
        if b_col not in cols:
            cursor.execute(f"ALTER TABLE company_settings ADD COLUMN {b_col} BLOB")
    if 'tin_number' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN tin_number TEXT")
    if 'footer' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN footer TEXT")
    if 'vat_rate' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN vat_rate REAL DEFAULT 18.0")
    if 'wht_rate' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN wht_rate REAL DEFAULT 6.0")
    if 'profit_margin' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN profit_margin REAL DEFAULT 50.0")
    if 'retention_rate' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN retention_rate REAL DEFAULT 5.0")
    if 'performance_bond_rate' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN performance_bond_rate REAL DEFAULT 10.0")
    if 'discount_rate' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN discount_rate REAL DEFAULT 0.0")
    if 'currency' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN currency TEXT DEFAULT 'UGX'")
    if 'vwht_rate' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN vwht_rate REAL DEFAULT 6.0")
    if 'reg_number' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN reg_number TEXT")
    if 'website' not in cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN website TEXT")
            
    # Check users table columns (upgrade support)
    cursor.execute("PRAGMA table_info(users)")
    u_cols = [c[1] for c in cursor.fetchall()]
    if 'password' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN password TEXT")
    if 'permissions' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT '{}'")
    if 'biometrics_enabled' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN biometrics_enabled INTEGER DEFAULT 0")
    if 'email' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if 'google_email' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN google_email TEXT")
    if 'username' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if 'phone' not in u_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN phone TEXT")

    # Add user_id (owner) columns to data tables for per-user isolation
    cursor.execute("PRAGMA table_info(client_list)")
    cl_cols = [c[1] for c in cursor.fetchall()]
    if 'user_id' not in cl_cols:
        cursor.execute("ALTER TABLE client_list ADD COLUMN user_id INTEGER")

    cursor.execute("PRAGMA table_info(area_list)")
    al_cols = [c[1] for c in cursor.fetchall()]
    if 'user_id' not in al_cols:
        cursor.execute("ALTER TABLE area_list ADD COLUMN user_id INTEGER")

    cursor.execute("PRAGMA table_info(sales)")
    sl_cols = [c[1] for c in cursor.fetchall()]
    if 'user_id' not in sl_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN user_id INTEGER")

    # Assign all existing data (with no owner) to the first admin user
    first_admin = cursor.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
    if first_admin:
        admin_id = first_admin[0]
        cursor.execute("UPDATE client_list SET user_id = ? WHERE user_id IS NULL", (admin_id,))
        cursor.execute("UPDATE area_list SET user_id = ? WHERE user_id IS NULL", (admin_id,))
        cursor.execute("UPDATE sales SET user_id = ? WHERE user_id IS NULL", (admin_id,))
 
    # Check sales table columns (upgrade support)
    cursor.execute("PRAGMA table_info(sales)")
    s_cols = [c[1] for c in cursor.fetchall()]
    if 'tax_invoice_date' not in s_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN tax_invoice_date TEXT")
    if 'tax_period' not in s_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN tax_period TEXT")
    
    # Performance Indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_client ON sales(client_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_area ON sales(area_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(payment_status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(completion_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_valid ON sales(payment_status, ownership_status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_ura ON sales(is_vat_rated, ura_status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sales_tax_date ON sales(tax_invoice_date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_clients_deleted ON client_list(delete_flag)')

    if not cursor.execute("SELECT * FROM company_settings").fetchone():
        cursor.execute("INSERT INTO company_settings (id, name) VALUES (1, '')")
    if not cursor.execute("SELECT * FROM system_info WHERE meta_field = 'name'").fetchone():
        cursor.execute("INSERT INTO system_info (meta_field, meta_value) VALUES ('name', 'ContractPro')")
    
    # Initialize dynamic user profile enrollment setting
    if not cursor.execute("SELECT * FROM system_info WHERE meta_field = 'profiles_enrolled'").fetchone():
        default_enrolled = '0' if is_new_db else '1'
        cursor.execute("INSERT INTO system_info (meta_field, meta_value) VALUES ('profiles_enrolled', ?)", (default_enrolled,))
    
    # Initialize or retrieve dynamic Flask app secret key (persisted across restarts)
    import secrets as _sec
    secret_row = cursor.execute("SELECT meta_value FROM system_info WHERE meta_field = 'secret_key'").fetchone()
    if not secret_row:
        new_secret = _sec.token_hex(32)
        cursor.execute("INSERT OR IGNORE INTO system_info (meta_field, meta_value) VALUES ('secret_key', ?)", (new_secret,))
        app.secret_key = new_secret
    else:
        app.secret_key = secret_row[0]

    # Ensure at least one admin exists
    if not cursor.execute("SELECT * FROM users WHERE is_admin = 1").fetchone():
        hashed_admin = generate_password_hash('admin')
        cursor.execute("INSERT INTO users (name, is_admin, password) VALUES ('Admin', 1, ?)", (hashed_admin,))

    # --- Migrate any legacy plaintext passwords to secure hashes ---
    plain_users = cursor.execute(
        "SELECT id, password FROM users WHERE password IS NOT NULL AND password != ''"
    ).fetchall()
    for pu in plain_users:
        pw = pu[1]
        # Werkzeug hashes start with 'scrypt:' or 'pbkdf2:'; bcrypt with '$2'
        if not (pw.startswith('scrypt:') or pw.startswith('pbkdf2:') or pw.startswith('$2')):
            new_hash = generate_password_hash(pw)
            cursor.execute("UPDATE users SET password = ? WHERE id = ?", (new_hash, pu[0]))
            print(f"[Security] Migrated plaintext password for user id={pu[0]} to secure hash.")

    # --- Upgrade legacy blank investments (which were saved as 0.0) to NULL ---
    cursor.execute("UPDATE sales SET investment_amount = NULL WHERE investment_amount = 0.0")

    conn.commit()

    # --- Database Integrity Check (log only, don't block) ---
    try:
        result = cursor.execute("PRAGMA integrity_check").fetchone()
        if result and result[0] != 'ok':
            print(f"[DB WARNING] integrity_check reported: {result[0]}")
        else:
            print(f"[DB] integrity_check passed.")
    except Exception as ie:
        print(f"[DB] integrity_check error: {ie}")

    conn.close()

GLOBAL_PORT = 5000

class JS_API:
    def open_project(self):
        result = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=('ContractPro Database (*.mdb;*.db)', 'All files (*.*)'))
        if result:
            return self.load_file(result[0])
        return False

    def open_external(self, url):
        import webbrowser
        webbrowser.open(url)
        return True

    def focus_search_input(self):
        """Force-focus the global search input via native evaluate_js call."""
        try:
            js = """(function(){
                var inp = document.getElementById('globalSearchInput');
                if(inp){ inp.removeAttribute('readonly'); inp.focus(); }
            })();"""
            webview.windows[0].evaluate_js(js)
        except Exception:
            pass
        return True
            
    def new_project(self):
        result = webview.windows[0].create_file_dialog(webview.SAVE_DIALOG, file_types=('ContractPro Database (*.mdb)',), save_filename='New_Database.mdb')
        if result:
            new_path = result
            if not new_path.endswith('.mdb'): new_path += '.mdb'
            if os.path.exists(new_path): os.remove(new_path)
            global CURRENT_DB
            CURRENT_DB = new_path
            init_db() 
            add_recent_file(new_path)
            return True
        return False
            
    def save_as(self):
        result = webview.windows[0].create_file_dialog(webview.SAVE_DIALOG, file_types=('ContractPro Database (*.mdb)',), save_filename='Database_Copy.mdb')
        if result:
            new_path = result
            if not new_path.endswith('.mdb'): new_path += '.mdb'
            shutil.copy2(get_db_path(), new_path)
            # For save as, we can still reload from python or let JS handle it
            self.load_file(new_path)
            return True
        return False

    def save_base64_image(self, b64_data, filename):
        result = webview.windows[0].create_file_dialog(webview.SAVE_DIALOG, file_types=('PNG Image (*.png)',), save_filename=filename)
        if result:
            import base64
            # Strip data:image/png;base64, prefix if present
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            try:
                save_path = result
                if not save_path.lower().endswith('.png'): save_path += '.png'
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                return True
            except Exception as e:
                logging.error(f"Error saving image: {e}")
                return False
        return False

    def load_file(self, path):
        global CURRENT_DB
        CURRENT_DB = path
        init_db()
        add_recent_file(path)
        return True

    def open_file(self, path):
        if os.path.exists(path):
            return self.load_file(path)
        return False

    def export_sales_xlsx(self, filters_json):
        try:
            import json, pandas as pd
            filters = json.loads(filters_json)
            
            # 1. Ask for location
            save_path = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, 
                file_types=('Excel Workbook (*.xlsx)',), 
                save_filename='Sales_Registry_Export.xlsx'
            )
            
            if not save_path: return False
            # Handle list return from some OS versions
            if isinstance(save_path, list): save_path = save_path[0]
            if not save_path.lower().endswith('.xlsx'): save_path += '.xlsx'
                
            # 2. Get Data
            client_id = filters.get('client_id')
            area_id = filters.get('area_id')
            status = filters.get('status')
            
            query = "SELECT * FROM sales WHERE 1=1"
            params = []
            if client_id:
                query += " AND client_id = ?"
                params.append(client_id)
            if area_id:
                query += " AND area_id = ?"
                params.append(area_id)
            if status == 'PAID':
                query += " AND payment_status = 'PAID'"
            elif status == 'NOT PAID':
                query += " AND (payment_status IS NULL OR payment_status != 'PAID')"
                
            db = get_db()
            rows = db.execute(query, params).fetchall()
            
            data = []
            for r in rows:
                c = db.execute("SELECT name FROM client_list WHERE id=?", (r['client_id'],)).fetchone()
                a = db.execute("SELECT name FROM area_list WHERE id=?", (r['area_id'],)).fetchone()
                data.append({
                    'Entry ID': r['entry_id'],
                    'Invoice': r['invoice_code'],
                    'Contract': r['contract_no'],
                    'PO': r['po_no'],
                    'Client': c['name'] if c else 'N/A',
                    'Area': a['name'] if a else 'N/A',
                    'Date': r['completion_date'],
                    'Details': r['contract_details'],
                    'Company': r['company_name'],
                    'Type': r['contract_type'],
                    'Gov': "Yes" if r['is_gov'] else "No",
                    'VAT Rated': "Yes" if r['is_vat_rated'] else "No",
                    'Status': r['payment_status'],
                    'Payment Date': r['payment_date'],
                    'URA Status': r['ura_status'],
                    'Ownership': r['ownership_status'],
                    'Total Amount': r['total'],
                    'Investment': r['investment_amount'],
                    'Created At': r['created_at']
                })
            
            pd.DataFrame(data).to_excel(save_path, index=False)
            return True
        except Exception as e:
            print(f"Export Error: {e}")
            return False

def run_server(port):
    import flask.cli; flask.cli.show_server_banner = lambda *args, **kwargs: None
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

@app.route('/api/export_sales')
def export_sales():
    # Use same filters as demands
    client_id = request.args.get('client_id')
    area_id = request.args.get('area_id')
    status = request.args.get('status')
    
    query = "SELECT * FROM sales WHERE 1=1"
    params = []
    if client_id:
        query += " AND client_id = ?"
        params.append(client_id)
    if area_id:
        query += " AND area_id = ?"
        params.append(area_id)
    if status == 'PAID':
        query += " AND payment_status = 'PAID'"
    elif status == 'NOT PAID':
        query += " AND (payment_status IS NULL OR payment_status != 'PAID')"
    
    db = get_db()
    rows = db.execute(query, params).fetchall()
    
    # Generate CSV
    import io, csv
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Entry ID', 'Invoice', 'Contract', 'PO', 'Client', 'Area', 'Date', 'Details', 
        'Company', 'Type', 'Gov', 'VAT Rated', 'Status', 'Payment Date', 
        'URA Status', 'Ownership', 'Total Amount', 'Investment', 'Created At'
    ])
    
    for r in rows:
        client_name = db.execute("SELECT name FROM client_list WHERE id=?", (r['client_id'],)).fetchone()['name']
        area_name = db.execute("SELECT name FROM area_list WHERE id=?", (r['area_id'],)).fetchone()['name']
        writer.writerow([
            r['entry_id'], r['invoice_code'], r['contract_no'], r['po_no'],
            client_name, area_name, r['completion_date'], r['contract_details'],
            r['company_name'], r['contract_type'], "Yes" if r['is_gov'] else "No",
            "Yes" if r['is_vat_rated'] else "No", r['payment_status'], r['payment_date'],
            r['ura_status'], r['ownership_status'], r['total'], r['investment_amount'],
            r['created_at']
        ])
    
    from flask import make_response
    res = make_response(output.getvalue())
    res.headers["Content-Disposition"] = "attachment; filename=sales_export.csv"
    res.headers["Content-type"] = "text/csv"
    return res

# --- INITIALIZE DATABASE AND ASSETS FOR WEB/CLOUD PRODUCTION ---
if GCS_BUCKET_NAME:
    download_db_from_gcs()
init_db()
if GCS_BUCKET_NAME:
    ensure_local_assets()

if __name__ == "__main__":
    # Redirect stdout/stderr to a log in the app data folder so we can inspect runtime crashes
    runtime_log = os.path.join(APP_DATA_DIR, 'launcher_runtime.log')
    try:
        logf = open(runtime_log, 'a', encoding='utf-8')
    except Exception:
        logf = None

    def log(msg):
        try:
            if logf:
                logf.write(msg + "\n")
                logf.flush()
        except Exception:
            pass

    try:
        log('--- starting app ---')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s: s.bind(('127.0.0.1', 5000))
            GLOBAL_PORT = 5000
        except:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s: s.bind(('', 0)); GLOBAL_PORT = s.getsockname()[1]

        # Setup on startup (already initialized globally above, but ensure argv DB is logged in desktop mode)
        if len(sys.argv) > 1 and ARG_DB_PATH:
            add_recent_file(ARG_DB_PATH)

        if webview is not None:
            threading.Thread(target=run_server, args=(GLOBAL_PORT,), daemon=True).start()
            start_daily_backup_scheduler()

            # Try to find icon for window
            icon_path = os.path.join(UPLOAD_FOLDER, "current_icon.png")
            if not os.path.exists(icon_path):
                icon_path = get_resource_path("static/icon.png")

            # Restore saved window geometry
            geom = get_window_geometry()
            win_w = geom.get('width') or 1280
            win_h = geom.get('height') or 800
            win_x = geom.get('x')  # None means OS decides
            win_y = geom.get('y')

            api = JS_API()
            log(f'Creating webview window on port {GLOBAL_PORT}, icon={icon_path}, geom={geom}')

            # Build native macOS menu using proper pywebview Menu/MenuAction/MenuSeparator objects.
            # The cocoa backend calls .title on each item, so plain dicts are not accepted.
            native_menu = []
            try:
                from webview.menu import Menu as WVMenu, MenuAction, MenuSeparator

                _port = GLOBAL_PORT  # capture port in closure

                def _load(path):
                    def _fn():
                        webview.windows[0].load_url(f'http://127.0.0.1:{_port}{path}')
                    return _fn

                def _js(script):
                    def _fn():
                        webview.windows[0].evaluate_js(script)
                    return _fn

                _about_js = (
                    "(function(){"
                    "var m=document.createElement('div');"
                    "m.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;"
                    "background:rgba(15,23,42,0.8);z-index:99999;display:flex;"
                    "align-items:center;justify-content:center;';"
                    "m.innerHTML='<div style=\"background:#1e293b;color:#fff;border-radius:16px;"
                    "padding:40px;max-width:380px;text-align:center;"
                    "box-shadow:0 25px 50px rgba(0,0,0,0.5);\">"
                    "<div style=\"font-size:3rem;margin-bottom:12px;\">&#x1F4CB;</div>"
                    "<h2 style=\"font-weight:800;margin-bottom:8px;\">ContractPro</h2>"
                    "<p style=\"color:#94a3b8;margin-bottom:20px;\">Contract &amp; Sales Management System</p>"
                    "<p style=\"color:#64748b;font-size:0.85rem;margin-bottom:24px;\">Secure &middot; Reliable &middot; Native</p>"
                    "<button onclick=\"this.closest(\\'div\\').parentNode.remove()\" "
                    "style=\"background:#3b82f6;color:#fff;border:none;padding:10px 28px;"
                    "border-radius:8px;font-weight:700;cursor:pointer;\">Close</button></div>';"
                    "document.body.appendChild(m);"
                    "})()"
                )

                native_menu = [
                    WVMenu('File', [
                        MenuAction('New Project',          lambda: api.new_project()),
                        MenuAction('Open Project',         lambda: api.open_project()),
                        MenuAction('Save As Copy',         lambda: api.save_as()),
                        MenuSeparator(),
                        MenuAction('Export Database (.mdb)', _load('/settings/export')),
                        MenuSeparator(),
                        MenuAction('Close Database',       _load('/close_database')),
                    ]),
                    WVMenu('View', [
                        MenuAction('Toggle Sidebar',  _js('toggleSidebar()')),
                        MenuAction('Reload',          _js('window.location.reload()')),
                        MenuSeparator(),
                        MenuAction('Dashboard',       _load('/')),
                        MenuAction('Sales Registry',  _load('/demands')),
                        MenuAction('Reports',         _load('/reports')),
                        MenuAction('Clients',         _load('/clients')),
                    ]),
                    WVMenu('Help', [
                        MenuAction('About ContractPro', _js(_about_js)),
                    ]),
                ]
            except Exception as menu_err:
                log(f'Menu build error (non-fatal): {menu_err}')
                native_menu = []

            # Create window with restored geometry
            create_kwargs = dict(
                title='ContractPro',
                url=f'http://127.0.0.1:{GLOBAL_PORT}',
                width=win_w,
                height=win_h,
                js_api=api,
            )
            # x/y are only passed when a valid saved position exists
            if win_x is not None:
                create_kwargs['x'] = win_x
            if win_y is not None:
                create_kwargs['y'] = win_y

            window = webview.create_window(**create_kwargs)

            # --- MAC OS NATIVE SYSTEM LOCK OBSERVER ---
            try:
                from AppKit import NSWorkspace
                from Foundation import NSObject, NSDistributedNotificationCenter
                
                class SystemLockObserver(NSObject):
                    def screenIsLocked_(self, notification):
                        log("macOS Event: Screen locked! Forcing logout...")
                        if len(webview.windows) > 0:
                            webview.windows[0].load_url(f'http://127.0.0.1:{GLOBAL_PORT}/users/logout')
                            
                    def systemWillSleep_(self, notification):
                        log("macOS Event: System going to sleep! Forcing logout...")
                        if len(webview.windows) > 0:
                            webview.windows[0].load_url(f'http://127.0.0.1:{GLOBAL_PORT}/users/logout')

                global _global_lock_observer # Keep alive to prevent GC segfault
                _global_lock_observer = SystemLockObserver.alloc().init()
                
                # Observe screen lock
                NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                    _global_lock_observer,
                    "screenIsLocked:",
                    "com.apple.screenIsLocked",
                    None
                )
                
                # Observe system sleep
                NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
                    _global_lock_observer,
                    "systemWillSleep:",
                    "NSWorkspaceWillSleepNotification",
                    None
                )
                log("Successfully registered macOS native Sleep & Lock observers.")
            except Exception as obs_err:
                log(f"Failed to setup macOS sleep/lock observers: {obs_err}")



            # Save window geometry on close
            def _on_closed():
                try:
                    save_window_geometry(window)
                    log('Window geometry saved on close.')
                except Exception as ge:
                    log(f'Failed to save geometry: {ge}')

            window.events.closed += _on_closed

            start_kwargs = {}
            if native_menu:
                try:
                    start_kwargs['menu'] = native_menu
                except Exception:
                    pass

            webview.start(**start_kwargs)
        else:
            log(f'PyWebView is not available. Running Flask server synchronously on http://127.0.0.1:{GLOBAL_PORT}')
            print(f'==============================================================')
            print(f'ContractPro is running in Pure Browser Mode.')
            print(f'Please open your browser and navigate to: http://127.0.0.1:{GLOBAL_PORT}')
            print(f'==============================================================')
            run_server(GLOBAL_PORT)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log('Unhandled exception: ' + str(e))
        log(tb)
        # Also print to stderr so macOS Console/consolidated logs may capture it
        print('Unhandled exception:', e, file=sys.stderr)
        print(tb, file=sys.stderr)
    finally:
        try:
            if logf:
                logf.close()
        except Exception:
            pass