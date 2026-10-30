# Agent icons

Show each agent's vendor logo (Claude, pi, Codex and others) in the Herdr sidebar Agents list.

The plugin writes the logo to the `$agent_icon` pane token. Your Herdr config decides where the token shows. The logos are glyphs of the "Herdr Agent Icons Max" font from [herdr-radar](https://github.com/hhdebb/herdr-radar) (MIT). The vendor marks belong to their owners; see herdr-radar's [THIRD_PARTY_NOTICES.md](https://github.com/hhdebb/herdr-radar/blob/v1.4.5/THIRD_PARTY_NOTICES.md).

## Install

Requirements: Herdr 0.9 or later, Python 3.11 or later, and Fontconfig (`fc-match`).

1. Install the plugin:

   ```bash
   herdr plugin install Nulluminati/herdr-extensions/plugins/agent-icons
   ```

   To work on the plugin from a clone, link it instead, so edits take effect at once:

   ```bash
   herdr plugin link ~/Projects/herdr-extensions/plugins/agent-icons
   ```

2. Install the font for your user:

   ```bash
   mkdir -p ~/.local/share/fonts
   curl -fsSL -o ~/.local/share/fonts/HerdrAgentIconsMax-Regular.ttf https://raw.githubusercontent.com/hhdebb/herdr-radar/v1.4.5/dist/HerdrAgentIconsMax-Regular.ttf
   fc-cache -f
   ```

3. Map the logo codepoints to the font in your terminal config.

   Ghostty (`~/.config/ghostty/config`):

   ```
   font-codepoint-map = U+E1A0-U+E1BA=Herdr Agent Icons Max
   ```

   kitty (`~/.config/kitty/kitty.conf`):

   ```
   symbol_map U+E1A0-U+E1BA Herdr Agent Icons Max
   ```

   Alacritty and other terminals with no codepoint map use Fontconfig fallback. Add the font as a fallback in `~/.config/fontconfig/conf.d/50-herdr-agent-icons.conf`, then run `fc-cache -f`:

   ```xml
   <?xml version="1.0"?>
   <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
   <fontconfig>
     <!-- Herdr agent logos: try this font before the system fallbacks. It has only Private Use Area glyphs. -->
     <match target="pattern">
       <edit name="family" mode="append" binding="strong">
         <string>Herdr Agent Icons Max</string>
       </edit>
     </match>
   </fontconfig>
   ```

   Open a new terminal window. The map and the rule do not apply to windows that are already open.

4. Add the token to the agent rows in `~/.config/herdr/config.toml`:

   ```toml
   [ui.sidebar.agents]
   rows = [["state_icon", "$agent_icon", "agent", "state_text"]]

   [ui.sidebar.agents.rows_by_agent]
   claude = [["state_icon", { token = "$agent_icon", fg = "#D97757" }, "agent"]]
   pi = [["state_icon", "$agent_icon", "agent"]]
   ```

   A `rows_by_agent` entry replaces `rows` for that agent, so put the token in each entry you have. Claude's color `#D97757` is the Anthropic coral. Other vendors use the row's default color unless you add `fg`.

5. Apply the config and write the tokens:

   ```bash
   herdr server reload-config
   herdr plugin action invoke nulluminati.agent-icons.refresh
   ```

## Use

There is nothing to do after the install. The plugin writes the token for every agent when the Herdr server starts, and for each new agent when Herdr detects it.

Run the `Agent icons: refresh` action (`nulluminati.agent-icons.refresh`) after you install the font or link the plugin.

## Known limits

- Font only. When `fc-match` does not find the font, the plugin clears the token and shows no icon. There is no text fallback.
- On macOS, the font check also uses `fc-match`. Without Fontconfig, there are no icons.
- The plugin cannot see your terminal's codepoint map. Boxes in place of logos mean the map (step 3) is missing.
- Other fonts also use these codepoints. STIX Two Math, for example, puts Greek letters there, so a Claude row shows λ. That also means step 3 is missing for your terminal.
- Agents with no logo in the font get no icon.
- `herdr plugin log list --plugin nulluminati.agent-icons` shows failed Herdr calls.

## Tests

```bash
uv run pytest tests/agent-icons
```

Run it from the repository root.
