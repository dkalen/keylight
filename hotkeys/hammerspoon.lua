-- Key Light hotkeys for Hammerspoon (https://www.hammerspoon.org).
-- Copy into ~/.hammerspoon/init.lua (or `require` it), then Reload Config.

local keylight = os.getenv("HOME") .. "/.local/bin/keylight"
local mods = {"cmd", "alt", "ctrl"}

local function kl(...)
  local args = {"-q", ...}
  hs.task.new(keylight, function(code, _, stderr)
    if code ~= 0 then
      -- exit 3 = unreachable, 4 = unsupported device, 2 = config/usage
      hs.alert.show("keylight: " .. (stderr ~= "" and stderr or ("exit " .. code)))
    end
  end, args):start()
end

hs.hotkey.bind(mods, "L",     function() kl("toggle") end)
hs.hotkey.bind(mods, "Up",    function() kl("brightness", "+10") end)
hs.hotkey.bind(mods, "Down",  function() kl("brightness", "-10") end)
