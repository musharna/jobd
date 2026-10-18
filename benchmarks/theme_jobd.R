# House ggplot2 theme + palette for jobd's benchmark figures.
#
# The first R code in this repo (`git ls-files '*.R'` was empty), so it is
# created here rather than reused. Every plotting script under benchmarks/
# sources this file; restyle here, never inline in a script.

theme_jobd <- function(base_size = 12) {
  ggplot2::theme_minimal(base_size = base_size) +
    ggplot2::theme(
      panel.grid.minor = ggplot2::element_blank(),
      legend.position = "bottom",
      # Two or more legends stack, one full-width row each, so the last label
      # is not pushed off the right edge at a fixed figure width.
      legend.box = "vertical",
      legend.spacing.y = grid::unit(0, "pt"),
      legend.box.spacing = grid::unit(6, "pt"),
      legend.margin = ggplot2::margin(0, 0, 0, 0),
      plot.title.position = "plot"
    )
}

# Agent benchmark verdicts (benchmarks/agent_qa/score.py): right green, wrong
# red, declined neutral grey; no_answer (no ANSWER line at all) orange, so a
# format failure is never read as either of its neighbours.
pal_verdict <- c(
  right = "#2E7D5B",
  wrong = "#C0392B",
  declined = "#BDBDBD",
  no_answer = "#E69F00"
)

# Per-run cost/time points: one neutral colour for every setup. Setups are
# named on the axis; a colour per setup would reuse green and grey, which mean
# right and declined in the verdict figures beside it.
jobd_point_fill <- "grey35"
jobd_point_outline <- "white"
jobd_point_size <- 1.8
jobd_point_stroke <- 0.5

# Text on verdict bars: dark on the light grey of declined, white elsewhere.
jobd_bar_text <- c(`TRUE` = "grey15", `FALSE` = "white")
