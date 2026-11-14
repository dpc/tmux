/* $OpenBSD$ */
/*
 * Copyright (c) 2026 tmux contributors
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION
 * OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
 * CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <sys/types.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include "tmux.h"

static const char *border_features[] = {
	"pen", "rect", "half", "center", "palette", "style", "round",
	"priority", "outside", "radius"
};

static int
border_number(const char *s, u_int min, u_int max, u_int *n)
{
	u_int v = 0;

	if (*s == '\0')
		return (0);
	for (; *s; s++) {
		if (*s < '0' || *s > '9' || v > max / 10)
			return (0);
		v = v * 10 + *s - '0';
		if (v > max)
			return (0);
	}
	if (v < min)
		return (0);
	*n = v;
	return (1);
}

static int
border_colour(const char *s, int outside, int *colour)
{
	u_int n;
	size_t i;

	if (!outside && strcmp(s, "fg") == 0) {
		*colour = 8;
		return (1);
	}
	if (outside && strcmp(s, "default") == 0) {
		*colour = 8;
		return (1);
	}
	if (*s == 'p' && border_number(s + 1, 0, 255, &n)) {
		*colour = COLOUR_FLAG_256 | n;
		return (1);
	}
	if (strlen(s) != 6)
		return (0);
	for (i = 0; i < 6; i++) {
		if (!((s[i] >= '0' && s[i] <= '9') ||
		    (s[i] >= 'a' && s[i] <= 'f') ||
		    (!outside && s[i] >= 'A' && s[i] <= 'F')))
			return (0);
	}
	*colour = COLOUR_FLAG_RGB | strtoul(s, NULL, 16);
	return (1);
}

/* Parse into a temporary value: callers never see a partly valid command. */
int
cell_border_parse(const char *data, size_t len, struct cell_border_command *cmd)
{
	char buf[257], *fields[129], *p, *key, *value;
	const char *names[] = { "q", "pen", "pen-clear", "set", "clear",
	    "pen-round", "pen-square", "round", "square" };
	u_int n, count = 0, i, at, maskmax;
	int stroke, rect, joint;

	if (len == 0 || len > 256 || memchr(data, '\0', len) != NULL)
		return (0);
	memcpy(buf, data, len);
	buf[len] = '\0';
	p = buf;
	while (count < nitems(fields) && (fields[count] = strsep(&p, ";")) != NULL)
		count++;
	if (p != NULL || count < 2 || strcmp(fields[0], "cell-border") != 0)
		return (0);
	memset(cmd, 0, sizeof *cmd);
	cmd->outside = -1;
	cmd->radius = -1;
	for (i = 0; i < nitems(names); i++)
		if (strcmp(names[i], fields[1]) == 0)
			break;
	if (i == nitems(names))
		return (0);
	cmd->op = i;
	if (i == CELL_BORDER_QUERY)
		return (count == 2);
	stroke = (i == CELL_BORDER_PEN || i == CELL_BORDER_SET);
	rect = (i == CELL_BORDER_SET || i == CELL_BORDER_CLEAR ||
	    i == CELL_BORDER_ROUND || i == CELL_BORDER_SQUARE);
	joint = (i >= CELL_BORDER_PEN_ROUND);
	maskmax = joint ? 255 : 4095;
	at = 2;
	if (at >= count || !border_number(fields[at++], 1, maskmax, &cmd->mask))
		return (0);
	if (stroke) {
		if (count < at + 4 ||
		    !border_number(fields[at++], 1, 64, &n))
			return (0);
		cmd->stroke.width = n;
		if (!border_colour(fields[at++], 0, &cmd->stroke.colour) ||
		    !border_number(fields[at++], 0, 1, &n))
			return (0);
		cmd->stroke.placement = n;
		if (!border_number(fields[at++], 0, 1, &n))
			return (0);
		cmd->stroke.layer = n;
	}
	if (rect) {
		if (count < at + 2 ||
		    !border_number(fields[at++], 1, 65535, &cmd->rows) ||
		    !border_number(fields[at++], 1, 65535, &cmd->cols))
			return (0);
	}
	if (!stroke && !joint)
		return (at == count);
	for (; at < count; at++) {
		key = fields[at];
		value = strchr(key, '=');
		if (value == NULL || value == key || value[1] == '\0')
			return (0);
		*value++ = '\0';
		for (p = key; *p; p++)
			if (!(*p >= 'a' && *p <= 'z') && *p != '-')
				return (0);
		for (p = value; *p; p++)
			if (!(*p >= 'a' && *p <= 'z') &&
			    !(*p >= '0' && *p <= '9') && *p != '-')
				return (0);
		if (stroke && strcmp(key, "style") == 0) {
			if (strcmp(value, "solid") == 0)
				cmd->stroke.style = 0;
			else if (strcmp(value, "double") == 0)
				cmd->stroke.style = 1;
			else if (strcmp(value, "dashed") == 0)
				cmd->stroke.style = 2;
			else if (strcmp(value, "dotted") == 0)
				cmd->stroke.style = 3;
		} else if (stroke && strcmp(key, "priority") == 0 &&
		    border_number(value, 0, 15, &n))
			cmd->stroke.priority = n;
		else if (joint && strcmp(key, "radius") == 0 &&
		    border_number(value, 1, 256, &n))
			cmd->radius = n;
		else if (joint && strcmp(key, "outside") == 0)
			border_colour(value, 1, &cmd->outside);
	}
	return (1);
}

void
cell_border_apply(struct cell_border *border,
    const struct cell_border_command *cmd)
{
	u_int i;

	switch (cmd->op) {
	case CELL_BORDER_PEN:
	case CELL_BORDER_SET:
		for (i = 0; i < 12; i++)
			if (cmd->mask & (1U << i))
				border->strokes[i] = cmd->stroke;
		border->mask |= cmd->mask;
		break;
	case CELL_BORDER_PEN_CLEAR:
	case CELL_BORDER_CLEAR:
		border->mask &= ~cmd->mask;
		for (i = 0; i < 12; i++)
			if (cmd->mask & (1U << i))
				memset(&border->strokes[i], 0, sizeof border->strokes[i]);
		break;
	default:
		if (cmd->op == CELL_BORDER_PEN_ROUND || cmd->op == CELL_BORDER_ROUND)
			border->joints |= cmd->mask;
		else
			border->joints &= ~cmd->mask;
		if (cmd->outside != -1) {
			border->outside = cmd->outside;
			border->has_outside = 1;
		}
		if (cmd->radius != -1)
			border->radius = cmd->radius;
		break;
	}
}

int
cell_border_empty(const struct cell_border *border)
{
	return (border->mask == 0 && border->joints == 0 &&
	    !border->has_outside && border->radius == 0);
}

void
cell_border_reset_grid(struct grid *gd)
{
	struct grid_cell gc;
	u_int x, y;

	if (gd == NULL)
		return;
	memset(&gd->border_pen, 0, sizeof gd->border_pen);
	for (y = 0; y < gd->hsize + gd->sy; y++) {
		if (!(gd->linedata[y].flags & GRID_LINE_EXTENDED))
			continue;
		for (x = 0; x < gd->linedata[y].cellsize; x++) {
			grid_get_cell(gd, x, y, &gc);
			if (cell_border_empty(&gc.border))
				continue;
			memset(&gc.border, 0, sizeof gc.border);
			grid_set_cell(gd, x, y, &gc);
		}
	}
}

/* All attached clients viewing this window must be able to render the reply. */
u_int
cell_border_features(struct window_pane *wp)
{
	struct client *c;
	u_int features = CELL_BORDER_ALL;
	int found = 0;

	if (wp == NULL)
		return (0);
	TAILQ_FOREACH(c, &clients, entry) {
		if (c->session == NULL || (c->flags & CLIENT_CONTROL) ||
		    !session_has(c->session, wp->window))
			continue;
		found = 1;
		features &= c->tty.border_features;
	}
	if (!found || (features & CELL_BORDER_BASE) != CELL_BORDER_BASE)
		return (0);
	return (features);
}

void
cell_border_feature_string(u_int features, char *buf, size_t size)
{
	u_int i;

	*buf = '\0';
	for (i = 0; i < nitems(border_features); i++) {
		if (!(features & (1U << i)))
			continue;
		if (*buf)
			strlcat(buf, ",", size);
		strlcat(buf, border_features[i], size);
	}
}

/* Consume only this namespace, including malformed replies, never as keys. */
int
cell_border_reply(struct tty *tty, const char *buf, size_t len, size_t *size)
{
	const char prefix[] = "\033P1+bcell-border;";
	char payload[257], *p, *feature;
	size_t i, plen = sizeof prefix - 1;
	u_int bits = 0, j;

	if (tty->border_discard) {
		for (i = 0; i < len; i++) {
			if (tty->border_escape && buf[i] == '\\') {
				tty->border_discard = tty->border_escape = 0;
				*size = i + 1;
				return (0);
			}
			tty->border_escape = (buf[i] == '\033');
		}
		*size = len;
		return (0);
	}
	if (memcmp(buf, prefix, len < plen ? len : plen) != 0)
		return (-1);
	if (len < plen)
		return (2);
	for (i = plen; i + 1 < len; i++)
		if (buf[i] == '\033' && buf[i + 1] == '\\')
			break;
	if (i + 1 == len || i == len) {
		if (len > plen + 244) {
			tty->border_discard = 1;
			tty->border_escape = (buf[len - 1] == '\033');
			*size = len;
			return (0);
		}
		return (1);
	}
	*size = i + 2;
	if (i - plen > 244 || memchr(buf + plen, '\0', i - plen) != NULL)
		return (0);
	memcpy(payload, buf + plen, i - plen);
	payload[i - plen] = '\0';
	if (strncmp(payload, "ok;1;", 5) != 0)
		return (0);
	p = payload + 5;
	for (i = 0; p[i]; i++)
		if ((u_char)p[i] < 0x21 || (u_char)p[i] > 0x7e || p[i] == ';')
			return (0);
	while ((feature = strsep(&p, ",")) != NULL) {
		if (*feature == '\0')
			return (0);
		for (j = 0; j < nitems(border_features); j++)
			if (strcmp(feature, border_features[j]) == 0)
				bits |= 1U << j;
	}
	if ((bits & CELL_BORDER_BASE) == CELL_BORDER_BASE) {
		tty->border_features = bits;
		server_redraw_client(tty->client);
	}
	return (0);
}

static void
border_format_colour(struct tty *tty, const struct tty_style_ctx *style_ctx,
    int colour, int outside, char *buf, size_t size)
{
	int mapped;

	if (colour == 8)
		colour = outside ? style_ctx->defaults->bg : style_ctx->defaults->fg;
	mapped = colour_palette_get(style_ctx->palette, colour);
	if (mapped != -1)
		colour = mapped;
	colour = tty_map_theme_colour(tty, colour);
	if (colour & COLOUR_FLAG_RGB)
		xsnprintf(buf, size, "%06x", colour & 0xffffff);
	else if (colour & COLOUR_FLAG_256)
		xsnprintf(buf, size, "p%d", colour & 255);
	else if (colour >= 0 && colour <= 7)
		xsnprintf(buf, size, "p%d", colour);
	else if (colour >= 90 && colour <= 97)
		xsnprintf(buf, size, "p%d", colour - 90 + 8);
	else
		strlcpy(buf, outside ? "default" : "fg", size);
}

/*
 * Text redraw runs with an empty outer pen. Restore physical cell attributes
 * afterwards: this also handles two different borders on the halves of a wide
 * glyph. Coordinates are the already clipped terminal coordinates, not the
 * application's cursor. Never change the outer pen.
 */
void
cell_border_draw(struct tty *tty, struct screen *s, u_int px, u_int py,
    u_int nx, u_int atx, u_int aty, const struct tty_style_ctx *style_ctx)
{
	struct grid_cell gc;
	struct cell_border *border;
	struct cell_border_stroke *stroke;
	static const char *styles[] = { "solid", "double", "dashed", "dotted" };
	char buf[257], colour[16], outside[32], radius[32];
	u_int x, i;

	if ((tty->border_features & CELL_BORDER_BASE) != CELL_BORDER_BASE)
		return;
	for (x = 0; x < nx && atx + x < tty->sx && aty < tty->sy; x++) {
		if (server_client_ranges_is_empty(
		    tty_check_overlay_range(tty, atx + x, aty, 1)))
			continue;
		grid_view_get_cell(s->grid, px + x, py, &gc);
		border = &gc.border;
		if (cell_border_empty(border))
			continue;
		tty_cursor(tty, atx + x, aty);
		for (i = 0; i < 12; i++) {
			if (!(border->mask & (1U << i)))
				continue;
			stroke = &border->strokes[i];
			border_format_colour(tty, style_ctx, stroke->colour, 0,
			    colour, sizeof colour);
			xsnprintf(buf, sizeof buf,
			    "\033P1+bcell-border;set;%u;%u;%s;%u;%u;1;1"
			    ";style=%s;priority=%u\033\\",
			    1U << i, stroke->width, colour, stroke->placement,
			    stroke->layer, styles[stroke->style], stroke->priority);
			tty_puts(tty, buf);
		}
		if (border->joints || border->has_outside || border->radius) {
			*outside = *radius = '\0';
			if (border->has_outside) {
				border_format_colour(tty, style_ctx, border->outside,
				    1, colour, sizeof colour);
				xsnprintf(outside, sizeof outside, ";outside=%s", colour);
			}
			if (border->radius)
				xsnprintf(radius, sizeof radius, ";radius=%u", border->radius);
			xsnprintf(buf, sizeof buf,
			    "\033P1+bcell-border;%s;%u;1;1%s%s\033\\",
			    border->joints ? "round" : "square",
			    border->joints ? border->joints : 255, outside, radius);
			tty_puts(tty, buf);
		}
	}
}
