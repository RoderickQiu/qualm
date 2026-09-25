---
name: Something went wrong
about: A wrong pop-up, a missed one, setup or the app misbehaving
labels: bug
---

**What happened, and what you expected**


**Your Mac and Qualm**
Paste the output of these two commands (in Terminal; with Qualm.app, `qualm` is set up by the
setup window, or use `/Applications/Qualm.app/Contents/MacOS/Qualm -m qualm`):

```
qualm version
qualm doctor
```

**Qualm stopped working, or quit?** `qualm logs` prints the last lines of its log. The log has a
line for each screen Qualm judged (its title and address), so paste only the lines about the problem.

**A wrong or missed pop-up?** `qualm review --last 5` or the dashboard's Review tab shows what the model read
and how sure it was. Paste only what you're happy to share: titles and addresses from your own
screen are yours.
