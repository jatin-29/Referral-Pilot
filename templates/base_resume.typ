// ReferralPilot base resume -- Typst fallback (used when no LaTeX engine is installed).
// The content is read from resume.json, written next to this file by the tailor, so
// the same ranked/bolded data as the LaTeX template is used and no escaping is needed.

#let data = json("resume.json")
#let accent = rgb("#1F3A5F")

#set document(title: data.name + " - Resume", author: data.name)
#set page(paper: "a4", margin: (x: 1.4cm, top: 1.1cm, bottom: 1.1cm))
#set text(size: 9.5pt, font: ("New Computer Modern", "Libertinus Serif", "Linux Libertine"))
#set par(leading: 0.5em, justify: false)
#set list(indent: 0.4em, body-indent: 0.5em, spacing: 0.45em)

#let rich(spans) = {
  for s in spans {
    if s.bold { strong(s.text) } else { s.text }
  }
}

#let items(values) = {
  for (i, v) in values.enumerate() {
    if i > 0 { ", " }
    if v.bold { strong(v.text) } else { v.text }
  }
}

#let section(title) = {
  v(6pt)
  text(size: 11pt, weight: "bold", fill: accent, upper(title))
  v(-6pt)
  line(length: 100%, stroke: 0.6pt + accent)
  v(-1pt)
}

#let row(left, right) = grid(columns: (1fr, auto), column-gutter: 8pt, left, right)

#let bullets(entries) = list(..entries.map(b => rich(b)))

// ------------------------------------------------------------------ header ---
#align(center)[
  #text(size: 18pt, weight: "bold", data.name)
  #v(-4pt)
  #text(size: 9pt)[
    #for (i, c) in data.contact.enumerate() {
      if i > 0 { [ #h(4pt)|#h(4pt) ] }
      if c.url != none { link(c.url, c.text) } else { c.text }
    }
  ]
]

// ---------------------------------------------------------------- sections ---
#for name in data.sections {
  if name == "summary" {
    section("Summary")
    rich(data.summary)
  } else if name == "education" {
    section("Education")
    for edu in data.education {
      row(strong(edu.institution), edu.dates)
      let degree = if edu.score != "" { edu.degree + " | " + edu.score } else { edu.degree }
      row(emph(degree), emph(edu.location))
      if edu.coursework.len() > 0 {
        [Relevant coursework: #items(edu.coursework)]
        parbreak()
      }
    }
  } else if name == "skills" {
    section("Technical Skills")
    grid(
      columns: (auto, 1fr),
      column-gutter: 10pt,
      row-gutter: 5pt,
      ..data.skills.map(s => (strong(s.category), items(s.items))).flatten(),
    )
  } else if name == "experience" {
    section("Experience")
    for job in data.experience {
      let where = if job.location != "" { job.company + ", " + job.location } else { job.company }
      row([#strong(job.role) | #where], job.dates)
      bullets(job.bullets)
    }
  } else if name == "projects" {
    section("Projects")
    for project in data.projects {
      let label = if project.link != "" { link(project.link, project.link_label) } else { [] }
      row([#strong(project.name) | #items(project.tech)], label)
      bullets(project.bullets)
    }
  } else if name == "achievements" {
    section("Achievements")
    bullets(data.achievements)
  }
}
