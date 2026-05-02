"""
pytraceview/grouping_dialog.py
Reusable "Group Channels / Columns" dialog.

Can be used by any app that manages a channel list — live channel panels,
import dialogs, or any future context where grouping by unit, pattern, or
enabled state is useful.

Constructor:
    GroupingDialog(existing_group_names, accent_color="", parent=None)

    existing_group_names : set[str]  — already-used names (for collision avoidance)
    accent_color         : str       — hex colour for the Apply button; falls back
                                       to a neutral blue if empty
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QRadioButton, QButtonGroup,
)


class GroupingDialog(QDialog):
    """Dialog for grouping channels/columns by unit, wildcard pattern, or
    enabled state.  Fully inherits the application-level stylesheet — no
    hardcoded colours beyond the accent on the Apply button.
    """

    def __init__(self, existing_group_names: set = None,
                 accent_color: str = "", parent=None):
        super().__init__(parent)
        self._existing = existing_group_names or set()
        self.setWindowTitle("Group Channels")
        self.setMinimumWidth(500)
        self.setModal(True)

        accent = accent_color or "#2060a0"

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Grouping method ────────────────────────────────────────────────────
        method_box = QGroupBox("Grouping method")
        ml = QVBoxLayout(method_box)
        ml.setSpacing(6)

        self.radio_unit = QRadioButton(
            "By Unit  —  group channels by their assigned unit (V, °C, A, …)")
        self.radio_unit.setChecked(True)
        ml.addWidget(self.radio_unit)

        pat_row = QHBoxLayout()
        self.radio_pattern = QRadioButton("By Pattern:")
        pat_row.addWidget(self.radio_pattern)
        self.edit_pattern = QLineEdit()
        self.edit_pattern.setPlaceholderText("e.g.  3??   or   internal*")
        self.edit_pattern.setEnabled(False)
        self.edit_pattern.setToolTip(
            "Wildcard: * matches any text, ? matches one character.\n"
            "Case-insensitive.  Matched against the channel display label.")
        pat_row.addWidget(self.edit_pattern)
        ml.addLayout(pat_row)

        self.radio_enabled = QRadioButton(
            "Group enabled (visible) channels  —  "
            "collect all currently-checked channels into one group")
        ml.addWidget(self.radio_enabled)

        bg_method = QButtonGroup(self)
        bg_method.addButton(self.radio_unit)
        bg_method.addButton(self.radio_pattern)
        bg_method.addButton(self.radio_enabled)
        self.radio_pattern.toggled.connect(self.edit_pattern.setEnabled)

        layout.addWidget(method_box)

        # ── Mode ──────────────────────────────────────────────────────────────
        mode_box = QGroupBox("When matches are found")
        mode_l = QVBoxLayout(mode_box)
        mode_l.setSpacing(6)
        self.radio_create_new = QRadioButton(
            "Create new group(s)  —  matched channels leave their current group")
        self.radio_create_new.setChecked(True)
        mode_l.addWidget(self.radio_create_new)
        self.radio_create_inside = QRadioButton(
            "Create sub-group inside existing group  —  "
            "unmatched channels stay in the original")
        mode_l.addWidget(self.radio_create_inside)
        bg_mode = QButtonGroup(self)
        bg_mode.addButton(self.radio_create_new)
        bg_mode.addButton(self.radio_create_inside)
        layout.addWidget(mode_box)

        # ── Optional name override ─────────────────────────────────────────────
        name_box = QGroupBox("New Group Name  (optional)")
        nl = QHBoxLayout(name_box)
        nl.addWidget(QLabel("Name:"))
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("Leave blank for auto-generated name")
        self.edit_name.setToolTip(
            "Override the auto-generated group name.\n"
            "If the name already exists a _001 … _999 suffix is added.\n"
            "For multi-unit grouping this becomes a prefix: Name_V, Name_°C, …")
        nl.addWidget(self.edit_name)
        layout.addWidget(name_box)

        # ── Buttons ────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_apply  = QPushButton("Apply")
        btn_apply.setStyleSheet(
            f"background: {accent}; color: white; "
            f"padding: 4px 16px; font-weight: bold;")
        btn_apply.setDefault(True)
        btn_cancel.clicked.connect(self.reject)
        btn_apply.clicked.connect(self.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_apply)
        layout.addLayout(btn_row)

    def get_config(self):
        """Return (method, pattern, create_inside, custom_name).
        method: 'unit' | 'pattern' | 'enabled'"""
        if self.radio_unit.isChecked():
            method = "unit"
        elif self.radio_pattern.isChecked():
            method = "pattern"
        else:
            method = "enabled"
        pattern       = self.edit_pattern.text().strip()
        create_inside = self.radio_create_inside.isChecked()
        custom_name   = self.edit_name.text().strip()
        return method, pattern, create_inside, custom_name
