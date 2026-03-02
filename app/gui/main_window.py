from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QWidget,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("hourBTC Kalshi 1H Bot")
        root = QWidget()
        layout = QGridLayout(root)

        self.env = QLineEdit("paper")
        self.api_key = QLineEdit()
        self.key_file = QLineEdit()
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)

        browse = QPushButton("Pick .key")
        browse.clicked.connect(self.pick_key)

        layout.addWidget(QLabel("Environment (paper/production)"), 0, 0)
        layout.addWidget(self.env, 0, 1)
        layout.addWidget(QLabel("API key ID"), 1, 0)
        layout.addWidget(self.api_key, 1, 1)
        layout.addWidget(QLabel("Private key file"), 2, 0)
        layout.addWidget(self.key_file, 2, 1)
        layout.addWidget(browse, 2, 2)
        layout.addWidget(QPushButton("Save Credentials"), 3, 0)
        layout.addWidget(QPushButton("Start Bot"), 3, 1)
        layout.addWidget(QPushButton("Stop Bot"), 3, 2)
        layout.addWidget(self.logs, 4, 0, 1, 3)

        self.setCentralWidget(root)

    def pick_key(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select private key", "", "Key Files (*.key *.pem)")
        if file_name:
            self.key_file.setText(file_name)
