"""Ein ruhiges, kontrastreiches Desktop-Design."""

STYLE = """
QWidget { color: #e8eef7; font-family: 'Noto Sans', 'DejaVu Sans'; font-size: 13px; }
QMainWindow, QDialog { background: #0e131c; }
QWidget#central { background: #0e131c; }
QFrame#sidebar { background: #111823; border-right: 1px solid #243043; }
QFrame#card { background: #161e2b; border: 1px solid #293449; border-radius: 14px; }
QFrame#hero { background: #152b29; border: 1px solid #2b5046; border-radius: 16px; }
QFrame#notice { background: #242332; border: 1px solid #46415e; border-radius: 10px; }
QLabel { background: transparent; }
QLabel#brand { font-size: 19px; font-weight: 700; letter-spacing: -0.5px; }
QLabel#pageTitle { font-size: 29px; font-weight: 700; }
QLabel#heading { font-size: 17px; font-weight: 600; }
QLabel#heroVersion { font-size: 42px; font-weight: 700; color: #a0f3cb; }
QLabel#eyebrow { font-size: 11px; color: #9ce5c1; font-weight: 700; letter-spacing: 1px; }
QLabel#muted { color: #9aa9bf; }
QLabel#small { font-size: 11px; color: #8799b2; }
QLabel#value { font-size: 19px; font-weight: 600; }
QLabel#badge { background: #284b3e; color: #a0f3cb; border-radius: 6px; padding: 5px 10px; font-size: 11px; font-weight: 600; }
QLabel#error { color: #ffb4b4; }
QLabel#success { color: #a0f3cb; }
QPushButton { background: #202c3d; border: 1px solid #34445e; border-radius: 8px;
              padding: 10px 16px; font-weight: 600; }
QPushButton:hover { background: #2b3b52; border-color: #6a83a7; }
QPushButton:pressed { background: #172336; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus { border: 1px solid #84e8bb; }
QPushButton:disabled { color: #607087; background: #182131; border-color: #273348; }
QPushButton#primary { background: #85e8b9; color: #10231b; border-color: #85e8b9; }
QPushButton#primary:hover { background: #a0f3cd; }
QPushButton#primary:disabled { background: #2a4c40; color: #86a296; border-color: #2a4c40; }
QPushButton#danger { color: #ffb8b8; border-color: #7b444c; background: #362330; }
QPushButton#danger:hover:enabled { color: #260d16; background: #ff7a92; border-color: #ffc1ce; }
QPushButton#danger:pressed:enabled { color: #260d16; background: #f45c79; border-color: #ff9db1; }
QPushButton#danger:disabled { color: #75636a; background: #241c25; border-color: #40313c; }
QPushButton#nav { text-align: left; background: transparent; border: none; padding: 13px 16px; color: #9aa9bf; }
QPushButton#nav:hover { background: #1b2535; }
QPushButton#nav:checked { background: #1f3833; color: #a0f3cb; }
QComboBox, QLineEdit { background: #101722; border: 1px solid #34415a; border-radius: 8px;
                       padding: 9px 12px; min-height: 19px; selection-background-color: #356d57; }
QLineEdit:disabled, QComboBox:disabled { color: #75839a; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox::down-arrow { image: url(__ARROW_ICON__); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: #1b2638; selection-background-color: #305646; border: 1px solid #40506b; }
QCheckBox { spacing: 10px; padding: 4px 0; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #50647f; border-radius: 4px; background: #101722; }
QCheckBox::indicator:checked { background: #85e8b9; border-color: #85e8b9; image: url(__CHECK_ICON__); }
QRadioButton { spacing: 8px; }
QRadioButton::indicator { width: 14px; height: 14px; border: 2px solid #516680; border-radius: 9px; }
QRadioButton::indicator:checked { background: #85e8b9; border-color: #85e8b9; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { border: 1px solid #2c3a50; border-radius: 9px; background: #161e2b; padding: 14px 16px; margin-bottom: 7px; }
QListWidget::item:selected { background: #1b3731; border-color: #6ccd9f; }
QListWidget::item:hover { border-color: #6a83a7; }
QListWidget::item:disabled { color: #77869b; background: #131b27; }
QPlainTextEdit { background: #0b111a; border: 1px solid #29364b; border-radius: 10px;
                 padding: 12px; font-family: 'DejaVu Sans Mono'; font-size: 11px; color: #b3c3d8; }
QProgressBar { border: none; border-radius: 4px; background: #253248; height: 8px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: #85e8b9; border-radius: 4px; }
QProgressBar#downloadProgress { height: 24px; min-height: 24px; max-height: 24px;
                               border-radius: 6px; color: #ffffff; font-weight: 700; }
QProgressBar#downloadProgress::chunk { background: #356d57; border-radius: 6px; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #34455e; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #243149; color: #e8eef7; border: 1px solid #455977; padding: 8px; }
QMessageBox { background: #161e2b; }
QMessageBox QLabel { min-width: 360px; }
QSplitter::handle { background: #263449; }
"""
