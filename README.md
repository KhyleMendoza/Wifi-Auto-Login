# WiFi Auto Login

At work our PCs connect to the internet through a Wi-Fi login page (a FortiGate captive portal). The problem is it almost never works on the first try. You open the browser, wait for the login page, type your username and password... and nothing. No internet. So you try again. And again. Sometimes the login page doesn't even show up, sometimes it does but the login doesn't go through, and sometimes it takes 10 or 20 tries before it finally connects.

I got tired of doing that by hand (and so did everyone else), so I made this app. It keeps trying for you until you're actually connected.

![Screenshot of the app](docs/screenshot.png)

## Download

Get `WiFiAutoLogin.exe` from the [Releases](../../releases) page. It's a single file, no install and no Python needed.

When you open it, Windows asks for admin rights. Say yes. The app needs it to reset the network (flushdns / release / renew) when it gets stuck. It will still try to log in without admin, it just can't do the reset part.

## How to use it

1. Open the app.
2. Enter your username and password. Tick "Remember password" if you don't want to type it every time.
3. Press **Start** and leave it alone.

It will keep trying until the internet works. If you want it to stay running and log you back in whenever you get disconnected, tick **Watch mode** before pressing Start.

## What it does

Pretty much what you'd do by hand, just without getting tired of it:

- **Gets the login page to show up.** It opens a list of websites (http and https) one after another until the FortiGate redirects to its login page. Each site gets several tries, and the order changes every round, because some sites trigger the portal and some just don't on a given day.
- **Logs in for you.** Once the login page shows up, it fills in your username and password and submits it.
- **Checks that it really worked.** The portal saying "OK" doesn't mean you're online, so it opens a real page to make sure. It only says connected when the internet actually works.
- **Resets the network when it gets stuck.** After every 3 failed attempts it runs flushdns, release and renew, then keeps going. It doesn't give up unless you set a limit.

## L&K-Office and L&K-Guest

It works on both. The app sees which Wi-Fi you're on and uses the settings for that one, so each network can have its own username, password and portal address. If you leave the portal address blank, it finds it by itself.

You can also pick which Wi-Fi the PC should be on and tick "Keep the PC on this Wi-Fi", so if Windows jumps to another network the app moves it back.

L&K-Guest has one more problem: even after you log in, websites often won't open because the guest network's DNS doesn't answer. With **Fix broken Wi-Fi DNS** ticked, the app switches the Wi-Fi to 8.8.8.8 / 1.1.1.1 after login, and puts it back to automatic when you go to a different network.

## Logs

Every attempt is written to `logs/wifi_login_YYYY-MM-DD.txt` next to the exe: which network you're on, your IP, the gateway, and what happened at each step. Useful when you want to know why it took 15 tries this morning. Your password never goes in the log. Old logs are deleted after 30 days.

## Settings

Everything is in the window, and you can change things while it's running (press **Save / Apply**): the list of sites it tries, how many tries per site, the wait between tries, when to reset the network, the portal address, and so on. Settings are saved in `config.ini` next to the exe.

Note: if you tick "Remember password", it's stored as plain text in `config.ini`. Don't share that file.

## Running from source

```
pip install requests
python wifi_login_gui.py
```

Or double-click `run_wifi_login.bat`. There's also a version with no window:

```
python wifi_login.py            # log in once and exit
python wifi_login.py --watch    # keep running
```

To build the exe yourself:

```
pip install pyinstaller requests
python -m PyInstaller --noconfirm WiFiAutoLogin.spec
```

It ends up in the `dist` folder. I tuned the build so it opens fast even on our older PCs, and only one copy can run at a time.

## Known limits

- Only tested on English Windows 10/11.
- If you don't choose an adapter, the network reset resets all of them, cable included.
