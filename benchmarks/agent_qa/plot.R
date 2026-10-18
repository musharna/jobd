# Figures for the agent benchmark, from analyze.py's scored.jsonl: the sweep
# rows (rescored, with validity) plus the two no-model baseline rows.
#
#   python benchmarks/agent_qa/analyze.py
#   Rscript benchmarks/agent_qa/plot.R [scored.jsonl] [outdir]
#
# Writes summary.tsv (verdicts, cost and time per setup; verdicts per setup x
# kind of question) and three figures:
#   agent_qa.png             right / wrong / declined per setup (the README one)
#   agent_qa_cost_time.png   cost and seconds per answer, every run
#   agent_qa_by_category.png right / wrong / declined per setup x kind of question
# Bars count valid answers, not questions (questions x runs per setup; one run
# per question for a baseline, which has no randomness). Every number
# on the figures is computed here from the log; nothing is typed in by hand.
# Green, red and grey mean right, wrong and declined in every figure; setups
# are told apart by their axis labels, not by colour.
#
# summary.tsv and the figures hold only counts, costs and times, so they can
# be committed; scored.jsonl holds the replies (live broker data) and cannot.

library(ggplot2)
library(jsonlite)

args <- commandArgs(trailingOnly = TRUE)
results_path <- if (length(args) >= 1) args[1] else "~/jobd-artifacts/agent_qa/scored.jsonl"
outdir <- if (length(args) >= 2) args[2] else "benchmarks/agent_qa"
source("benchmarks/theme_jobd.R")

all_rows <- jsonlite::stream_in(file(path.expand(results_path)), verbose = FALSE)
stopifnot(is.logical(all_rows$valid), !anyNA(all_rows$valid))
r <- all_rows[all_rows$valid, ]
stopifnot(all(r$verdict %in% names(pal_verdict)))

arm_labels <- c(
  prior = "Constant guess (no model)",
  best_constant = "Best constant, fit to the key",
  bare = "Claude alone",
  alt = "Claude + job CLI",
  mcp = "Claude + jobd-mcp"
)
model_arms <- c("bare", "alt", "mcp")
stopifnot(all(r$arm %in% names(arm_labels)))
r$arm_label <- factor(arm_labels[r$arm], levels = rev(arm_labels))
r$verdict <- factor(r$verdict, levels = names(pal_verdict))

cat_labels <- c(
  exit_code = "Exit code of a job",
  host = "Host that ran a job",
  wall_time = "Run time of a job",
  project_failures = "A project's failures on one day",
  log_line = "Last line of a job's log",
  fleet = "Routing and worker capabilities"
)
stopifnot(all(r$category %in% names(cat_labels)))

n_per_arm <- table(all_rows$arm[all_rows$arm %in% model_arms])
stopifnot(length(unique(n_per_arm)) == 1)
n_ans <- as.numeric(n_per_arm[1])
n_q <- length(unique(r$qid))
n_rep <- length(unique(r$rep[r$arm %in% model_arms]))
n_valid <- table(factor(r$arm, levels = names(arm_labels)))

# Tables first: the figures draw these numbers, and the README quotes them.
tally <- function(x) {
  data.frame(
    right = sum(x$verdict == "right"), wrong = sum(x$verdict == "wrong"),
    declined = sum(x$verdict == "declined"), no_answer = sum(x$verdict == "no_answer"),
    unknown_host = sum(x$unknown_host),
    # Repeats of one question are not independent: also count questions.
    questions_right_every_run = sum(tapply(x$verdict == "right", x$qid, all))
  )
}
per_arm <- do.call(rbind, lapply(names(arm_labels), function(a) {
  x <- r[r$arm == a, ]
  m <- a %in% model_arms # a baseline has no cost, time, tools or broker
  cbind(
    level = "arm", arm = a, category = "all",
    runs = sum(all_rows$arm == a), invalid = sum(all_rows$arm == a & !all_rows$valid), tally(x),
    cost_usd_mean = if (m) round(mean(x$cost_usd), 4) else NA,
    cost_usd_median = if (m) round(median(x$cost_usd), 4) else NA,
    cost_usd_total = if (m) round(sum(x$cost_usd), 2) else NA,
    seconds_mean = if (m) round(mean(x$seconds), 1) else NA,
    seconds_median = if (m) round(median(x$seconds), 1) else NA,
    seconds_max = if (m) max(x$seconds) else NA,
    tool_calls_mean = if (m) round(mean(lengths(x$tool_calls)), 2) else NA,
    broker_requests_mean = if (m) round(mean(x$broker_requests), 2) else NA,
    refused_requests = if (m) sum(lengths(x$refused_requests)) else NA
  )
}))
per_cat <- do.call(rbind, lapply(names(arm_labels), function(a) {
  do.call(rbind, lapply(names(cat_labels), function(k) {
    cbind(
      level = "arm_x_category", arm = a, category = k, runs = NA, invalid = NA,
      tally(r[r$arm == a & r$category == k, ]),
      cost_usd_mean = NA, cost_usd_median = NA, cost_usd_total = NA,
      seconds_mean = NA, seconds_median = NA, seconds_max = NA, tool_calls_mean = NA,
      broker_requests_mean = NA, refused_requests = NA
    )
  }))
}))
# na = "NA", not "": a line ending in tabs is trailing whitespace a formatter
# may strip, which would drop those columns.
write.table(rbind(per_arm, per_cat), file.path(outdir, "summary.tsv"),
  sep = "\t", quote = FALSE, row.names = FALSE, na = "NA"
)

setup_note <- sprintf("Claude Opus 5.5 in Claude Code, %d questions × %d runs.", n_q, n_rep)
key_note <- paste0(
  "Answers checked against the same broker the tools read, so this measures\n",
  "whether a lookup is carried out correctly, not whether the broker is right."
)
legend_counts <- function(v) {
  sprintf("%s (%s)", v, vapply(v, function(k) {
    paste(vapply(names(arm_labels), function(a) {
      as.character(sum(r$verdict == k & r$arm == a))
    }, ""), collapse = " / ")
  }, ""))
}

# Figure 1: one bar per setup, segments = verdict share, labelled with counts.
counts <- as.data.frame(table(arm_label = r$arm_label, verdict = r$verdict))
counts <- counts[counts$Freq > 0, ]
counts$share <- counts$Freq /
  as.numeric(n_valid[names(arm_labels)[match(counts$arm_label, arm_labels)]])
p1 <- ggplot(counts, aes(x = share, y = arm_label, fill = verdict)) +
  geom_col(width = 0.62, position = position_stack(reverse = TRUE)) +
  geom_text(
    aes(label = ifelse(share >= 0.05, Freq, ""), colour = verdict == "declined"),
    position = position_stack(vjust = 0.5, reverse = TRUE),
    fontface = "bold", size = 4.2, show.legend = FALSE
  ) +
  scale_colour_manual(values = jobd_bar_text, guide = "none") +
  # Slices too thin to hold a number are counted in the legend instead, per
  # setup in the order the title names.
  scale_fill_manual(
    values = pal_verdict, labels = legend_counts, drop = TRUE,
    name = "Answers (constant / best constant / alone / job CLI / jobd-mcp):"
  ) +
  # Two rows: at 8.5 in the three legend keys no longer fit on one.
  guides(fill = guide_legend(title.position = "top", title.hjust = 0.5, nrow = 2, byrow = TRUE)) +
  scale_x_continuous(labels = scales::percent, expand = c(0, 0)) +
  labs(
    x = sprintf("Share of answers: %d per Claude setup, %d per no-model baseline", n_ans, n_q),
    y = NULL,
    title = "Questions about a live jobd broker: right, wrong or declined",
    subtitle = setup_note,
    caption = key_note
  ) +
  theme_jobd(base_size = 13) +
  theme(
    panel.grid.major.y = element_blank(), plot.margin = margin(8, 24, 8, 8),
    plot.caption = element_text(hjust = 0, colour = "grey30"), plot.caption.position = "plot"
  )
# 8.5 in wide, not 11: the README shows this figure at 780 px, where an 11 in
# figure drew the axis and legend text at about 10 px. At 8.5 in it is about 13.
ggsave(file.path(outdir, "agent_qa.png"), p1, width = 8.5, height = 5.6, dpi = 200, bg = "white")

# Figure 2: cost and seconds per answer, one point per run, box over them.
mr <- r[r$arm %in% model_arms, ]
mr$arm_label <- droplevels(mr$arm_label)
long <- rbind(
  data.frame(arm_label = mr$arm_label, metric = "Cost per answer (US$, log scale)", value = mr$cost_usd),
  data.frame(arm_label = mr$arm_label, metric = "Seconds per answer (log scale)", value = mr$seconds)
)
p2 <- ggplot(long, aes(x = value, y = arm_label)) +
  geom_boxplot(width = 0.55, outlier.shape = NA, fill = "white", colour = "grey20") +
  geom_point(
    position = position_jitter(height = 0.18, seed = 1),
    shape = 21, size = jobd_point_size, stroke = jobd_point_stroke,
    fill = jobd_point_fill, colour = jobd_point_outline, alpha = 0.55
  ) +
  facet_wrap(~metric, ncol = 2, scales = "free_x") +
  scale_x_log10(labels = scales::label_number(drop0trailing = TRUE)) +
  labs(
    x = NULL, y = NULL,
    title = "Cost and time per answer",
    subtitle = paste0(
      setup_note, "\nOne point per run; the box spans the middle half, the line in it is the median."
    )
  ) +
  theme_jobd(base_size = 13) +
  theme(
    panel.grid.major.y = element_blank(), strip.text = element_text(hjust = 0, face = "bold"),
    plot.margin = margin(8, 24, 8, 8)
  )
ggsave(file.path(outdir, "agent_qa_cost_time.png"), p2, width = 12, height = 4.2, dpi = 200, bg = "white")

# Figure 3: verdicts per setup x kind of question; strips name the counts.
first <- r[r$arm == "prior", ]
n_cat <- vapply(names(cat_labels), function(k) length(unique(first$qid[first$category == k])), 0)
present <- names(cat_labels)[n_cat > 0]
strip <- setNames(
  sprintf("%s (%d questions)", cat_labels[present], n_cat[present]),
  present
)
r$cat_strip <- factor(strip[r$category], levels = strip)
cc <- as.data.frame(table(arm_label = r$arm_label, cat_strip = r$cat_strip, verdict = r$verdict))
cc <- cc[cc$Freq > 0, ]
p3 <- ggplot(cc, aes(x = Freq, y = arm_label, fill = verdict)) +
  geom_col(width = 0.62, position = position_stack(reverse = TRUE)) +
  geom_text(
    aes(label = Freq, colour = verdict == "declined"),
    position = position_stack(vjust = 0.5, reverse = TRUE),
    fontface = "bold", size = 3.4, show.legend = FALSE
  ) +
  scale_colour_manual(values = jobd_bar_text, guide = "none") +
  facet_wrap(~cat_strip, ncol = 1, scales = "free_x") +
  scale_fill_manual(values = pal_verdict, name = NULL, drop = TRUE) +
  scale_x_continuous(expand = c(0, 0)) +
  labs(
    x = "Answers", y = NULL, title = "Right, wrong or declined, by kind of question",
    subtitle = paste0(
      setup_note, "\nThe two baselines answer each question once, with no model."
    )
  ) +
  theme_jobd(base_size = 12) +
  theme(
    panel.grid.major.y = element_blank(), strip.text = element_text(hjust = 0, face = "bold"),
    plot.margin = margin(8, 24, 8, 8)
  )
ggsave(file.path(outdir, "agent_qa_by_category.png"), p3, width = 9, height = 13, dpi = 200, bg = "white")

print(per_arm)
