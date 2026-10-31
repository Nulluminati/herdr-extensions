---

kanban-plugin: board
repo: github.com/acme/api
merge: pr
linear-projects:
- id: p-plat
  name: Platform
  labels:
  - {id: l-api, name: 'repo:api'}
linear-issues:
- id: i5
  identifier: ENG-5

---

## Triage

- [ ] Crash on start #type/bug ^tbug


## Todo (3)

- [ ] Billing v2 #type/feature #billing #api 📅 2026-11-15 ^tpar
	Details of billing
	- [ ] Choose a provider
		Nested note
- [ ] Add invoice table [[#^tpar]] ^tkid
- [ ] Shipped to Linear #home → ENG-7 ^tpro
- [ ] No ID yet #x
- [ ] Bad id ^NOT-AN-ID


## Shipped

**Complete**
- [x] Old work ^tdon


***

## Archive

- [x] First archived ^tar1
- [x] Second archived ^tar2

%% kanban:settings
```
{"kanban-plugin":"board"}
```
%%
