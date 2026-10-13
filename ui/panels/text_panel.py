"""
ui/panels/text_panel.py
────────────────────────
Text input card — textarea + live character counter.
"""
import customtkinter as ctk
from core.constants import COLORS, FONTS
from ui.theme import card

# Shown in the text box at startup, so a first Generate has something to speak.
SAMPLE_TEXT = (
    "I feel that it has become such a big challenge in today's world because there's so much "
    "data available, social media available, and you get instant feedback, right? So I think in "
    "the past, a band would lock themselves in a shed or whatever it may be and then work on stuff "
    "and maybe their friends would listen or they would listen, but it would take months before "
    "the audience heard it. Today, you could literally record something in three seconds, put it "
    "up and get instant feedback. And so the feedback loop has got shorter. The time to create "
    "something has got shorter. It's easier to publish and it's easier to get criticism or "
    "feedback. And so do you say that creativity being stifled and, and is hampering creativity? "
    "Absolutely. And the beauty of it is because so much of what's being made is being made that "
    "way, that if we choose to make something not like that, it really stands out."
)


class TextPanel:
    def __init__(self, parent, row=None):
        if row is not None:
            frame = card(parent, row, "📝  Text Input")
            pack_fill = "x"
            pack_expand = False
            tb_height = 140
        else:
            frame = ctk.CTkFrame(
                parent, fg_color=COLORS["bg_card"],
                corner_radius=14, border_width=1, border_color=COLORS["border"]
            )
            frame.pack(fill="both", expand=True, padx=16, pady=(14, 6))
            
            ctk.CTkLabel(
                frame, text="📝  Text Input",
                font=FONTS["subtitle"],
                text_color=COLORS["text_primary"],
            ).pack(anchor="w", padx=16, pady=(14, 10))
            
            pack_fill = "both"
            pack_expand = True
            tb_height = 140

        self.text_input = ctk.CTkTextbox(
            frame, height=tb_height, font=FONTS["mono"],
            fg_color=COLORS["bg_input"],
            text_color=COLORS["text_primary"],
            border_color=COLORS["border"], border_width=1,
            corner_radius=10, wrap="word",
        )
        self.text_input.pack(fill=pack_fill, expand=pack_expand, padx=16, pady=(0, 4))
        self.text_input.insert("1.0", SAMPLE_TEXT)

        self._char_label = ctk.CTkLabel(
            frame, text="0 characters",
            font=FONTS["body_small"],
            text_color=COLORS["text_muted"],
        )
        self._char_label.pack(anchor="e", padx=20, pady=(0, 10))
        self.text_input.bind("<KeyRelease>", self._update)
        self._update()

    def _update(self, _event=None):
        count = len(self.text_input.get("1.0", "end-1c"))
        self._char_label.configure(
            text=f"{count:,} character{'s' if count != 1 else ''}"
        )

    def get_text(self) -> str:
        return self.text_input.get("1.0", "end-1c")
