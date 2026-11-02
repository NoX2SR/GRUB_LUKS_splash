/* unlock_theme.c - themed passphrase screen for cryptomount.  */
/*
 *  GRUB  --  GRand Unified Bootloader
 *  Copyright (C) 2026  Free Software Foundation, Inc.
 *
 *  GRUB is free software: you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation, either version 3 of the License, or
 *  (at your option) any later version.
 *
 *  GRUB is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with GRUB.  If not, see <http://www.gnu.org/licenses/>.
 */

/*
 * unlock_theme [-t THEME] [-u] SOURCE|UUID
 *
 * Draws a themed passphrase screen, reads the passphrase and hands it to
 * cryptomount.  If the theme or the video mode cannot be set up, falls back
 * to the plain cryptomount prompt so the disk can always be unlocked.
 */

#include <grub/dl.h>
#include <grub/misc.h>
#include <grub/mm.h>
#include <grub/err.h>
#include <grub/env.h>
#include <grub/file.h>
#include <grub/term.h>
#include <grub/time.h>
#include <grub/extcmd.h>
#include <grub/command.h>
#include <grub/i18n.h>
#include <grub/video.h>
#include <grub/bitmap.h>
#include <grub/bitmap_scale.h>
#include <grub/color.h>
#include <grub/font.h>
#include <grub/gfxmenu_view.h>
#include <grub/cryptodisk.h>

GRUB_MOD_LICENSE ("GPLv3+");

/* Prefer the mode the firmware already set, usually the native one.  */
#define DEFAULT_VIDEO_MODE	"keep;auto"
#define DEFAULT_MAX_TRIES	3
#define CURSOR_BLINK_MS		500

enum
  {
    STATE_PROMPT = 1 << 0,
    STATE_BUSY = 1 << 1,
    STATE_ERROR = 1 << 2,
    STATE_ALL = STATE_PROMPT | STATE_BUSY | STATE_ERROR
  };

enum element_kind
  {
    ELEMENT_BOX,
    ELEMENT_IMAGE,
    ELEMENT_LABEL,
    ELEMENT_PASSPHRASE
  };

enum align
  {
    ALIGN_LEFT,
    ALIGN_CENTER,
    ALIGN_RIGHT
  };

enum mask_mode
  {
    MASK_PER_CHAR,
    MASK_FIXED,
    MASK_HIDDEN
  };

enum background_mode
  {
    BACKGROUND_STRETCH,
    BACKGROUND_CROP,
    BACKGROUND_FIT,
    BACKGROUND_CENTER
  };

/* A coordinate such as "120", "50%", "50%-260" or "center+10".  */
struct coord
{
  int set;
  int center;
  int percent;
  int offset;
};

struct element
{
  enum element_kind kind;
  struct coord left, top, width, height;
  int show;
  char *text;
  char *file;
  char *font_name;
  char *mask;
  char *placeholder;
  enum align align;
  enum mask_mode mask_mode;
  int fixed_length;
  int padding;
  int border_width;
  grub_video_rgba_color_t color;
  grub_video_rgba_color_t background_color;
  grub_video_rgba_color_t border_color;
  grub_video_rgba_color_t placeholder_color;
  grub_video_rgba_color_t cursor_color;
  grub_font_t font;
  struct grub_video_bitmap *bitmap;
  struct element *next;
};

struct theme
{
  char *dir;
  char *gfxmode;
  char *background_image;
  enum background_mode background_mode;
  grub_video_rgba_color_t background_color;
  struct grub_video_bitmap *background;
  unsigned long max_tries;
  struct element *elements;
  struct element **tail;
};

/* What is currently on screen.  */
struct screen
{
  struct theme *theme;
  unsigned int width;
  unsigned int height;
  int state;
  int cursor_on;
  grub_size_t pass_len;
  const char *device;
  unsigned long attempt;
};

struct parser
{
  const char *path;
  const char *start;
  const char *p;
};

/* Keyword values of the theme file, e.g. "align = center".  */
struct keyword
{
  const char *name;
  int value;
};

static const struct keyword element_kinds[] =
  {
    {"box", ELEMENT_BOX},
    {"image", ELEMENT_IMAGE},
    {"label", ELEMENT_LABEL},
    {"passphrase", ELEMENT_PASSPHRASE},
    {NULL, 0}
  };

static const struct keyword aligns[] =
  {
    {"left", ALIGN_LEFT},
    {"center", ALIGN_CENTER},
    {"right", ALIGN_RIGHT},
    {NULL, 0}
  };

static const struct keyword mask_modes[] =
  {
    {"per-char", MASK_PER_CHAR},
    {"fixed", MASK_FIXED},
    {"hidden", MASK_HIDDEN},
    {NULL, 0}
  };

static const struct keyword background_modes[] =
  {
    {"stretch", BACKGROUND_STRETCH},
    {"crop", BACKGROUND_CROP},
    {"fit", BACKGROUND_FIT},
    {"center", BACKGROUND_CENTER},
    {NULL, 0}
  };

static grub_video_rgba_color_t
rgba (grub_uint8_t red, grub_uint8_t green, grub_uint8_t blue,
      grub_uint8_t alpha)
{
  grub_video_rgba_color_t c = { red, green, blue, alpha };
  return c;
}

/* Theme file parsing.  */

static grub_err_t
parse_error (struct parser *ps, const char *msg)
{
  const char *q;
  int line = 1;

  for (q = ps->start; q < ps->p; q++)
    if (*q == '\n')
      line++;
  return grub_error (GRUB_ERR_BAD_FILE_TYPE, "%s:%d: %s", ps->path, line, msg);
}

/* Prefixes the pending error message with the file and line.  */
static grub_err_t
annotate_error (struct parser *ps)
{
  char *msg = grub_strdup (grub_errmsg);
  grub_err_t err;

  grub_errno = GRUB_ERR_NONE;
  err = parse_error (ps, msg ? msg : N_("out of memory"));
  grub_free (msg);
  return err;
}

static void
skip_space (struct parser *ps)
{
  for (;;)
    {
      while (*ps->p && grub_isspace (*ps->p))
	ps->p++;
      if (*ps->p != '#')
	return;
      while (*ps->p && *ps->p != '\n')
	ps->p++;
    }
}

/* Property names accept both "-" and "_" as word separators.  */
static char *
read_name (struct parser *ps)
{
  const char *s = ps->p;
  char *name, *q;

  while (grub_isalnum (*ps->p) || *ps->p == '-' || *ps->p == '_')
    ps->p++;
  if (ps->p == s)
    {
      parse_error (ps, N_("name expected"));
      return NULL;
    }
  name = grub_strndup (s, ps->p - s);
  if (name == NULL)
    return NULL;
  for (q = name; *q; q++)
    if (*q == '_')
      *q = '-';
  return name;
}

static char *
read_value (struct parser *ps)
{
  const char *s;
  char *value, *out;

  if (*ps->p != '"')
    {
      s = ps->p;
      while (*ps->p && !grub_isspace (*ps->p) && *ps->p != '}')
	ps->p++;
      if (ps->p == s)
	{
	  parse_error (ps, N_("value expected"));
	  return NULL;
	}
      return grub_strndup (s, ps->p - s);
    }

  ps->p++;
  value = grub_malloc (grub_strlen (ps->p) + 1);
  if (value == NULL)
    return NULL;
  out = value;
  while (*ps->p && *ps->p != '"')
    {
      if (*ps->p == '\\' && ps->p[1])
	ps->p++;
      *out++ = *ps->p++;
    }
  if (*ps->p != '"')
    {
      grub_free (value);
      parse_error (ps, N_("unterminated string"));
      return NULL;
    }
  ps->p++;
  *out = '\0';
  return value;
}

static grub_err_t
parse_coord (const char *s, struct coord *c)
{
  const char *p = s;
  long n;

  c->set = 1;
  c->center = 0;
  c->percent = 0;
  c->offset = 0;

  if (grub_strncmp (p, "center", 6) == 0)
    {
      c->center = 1;
      p += 6;
    }
  else
    {
      n = grub_strtol (p, &p, 10);
      if (p == s)
	return grub_error (GRUB_ERR_BAD_ARGUMENT,
			   N_("invalid coordinate `%s'"), s);
      if (*p == '%')
	{
	  c->percent = n;
	  p++;
	}
      else
	c->offset = n;
    }

  /* grub_strtol does not accept a leading '+'.  */
  if (*p == '+' || *p == '-')
    {
      int sign = (*p == '-') ? -1 : 1;
      const char *q = ++p;

      c->offset += sign * (int) grub_strtoul (q, &p, 10);
      if (p == q)
	return grub_error (GRUB_ERR_BAD_ARGUMENT,
			   N_("invalid coordinate `%s'"), s);
    }

  if (*p != '\0')
    return grub_error (GRUB_ERR_BAD_ARGUMENT, N_("invalid coordinate `%s'"), s);
  return GRUB_ERR_NONE;
}

/* "always" or a comma separated list of "prompt", "busy" and "error".  */
static grub_err_t
parse_show (const char *s, int *show)
{
  const char *p = s;

  if (grub_strcmp (s, "always") == 0)
    {
      *show = STATE_ALL;
      return GRUB_ERR_NONE;
    }

  *show = 0;
  while (*p)
    {
      const char *end = grub_strchr (p, ',');
      grub_size_t len = end ? (grub_size_t) (end - p) : grub_strlen (p);

      if (len == 6 && grub_strncmp (p, "prompt", len) == 0)
	*show |= STATE_PROMPT;
      else if (len == 4 && grub_strncmp (p, "busy", len) == 0)
	*show |= STATE_BUSY;
      else if (len == 5 && grub_strncmp (p, "error", len) == 0)
	*show |= STATE_ERROR;
      else
	return grub_error (GRUB_ERR_BAD_ARGUMENT,
			   N_("invalid show value `%s'"), s);
      p += len;
      if (*p == ',')
	p++;
    }
  return GRUB_ERR_NONE;
}

static grub_err_t
parse_number (const char *s, int *n)
{
  const char *end;

  *n = grub_strtoul (s, &end, 0);
  if (end == s || *end != '\0')
    return grub_error (GRUB_ERR_BAD_ARGUMENT, N_("invalid number `%s'"), s);
  return GRUB_ERR_NONE;
}

/* Sets *VALUE to what keyword S means in TABLE.  WHAT names the setting
   for the error message.  */
static grub_err_t
parse_keyword (const struct keyword *table, const char *what, const char *s,
	       int *value)
{
  for (; table->name; table++)
    if (grub_strcmp (table->name, s) == 0)
      {
	*value = table->value;
	return GRUB_ERR_NONE;
      }
  return grub_error (GRUB_ERR_BAD_ARGUMENT, N_("invalid %s `%s'"), what, s);
}

static grub_err_t
replace_string (char **dst, const char *value)
{
  char *copy = grub_strdup (value);

  if (copy == NULL)
    return grub_errno;
  grub_free (*dst);
  *dst = copy;
  return GRUB_ERR_NONE;
}

/* File names are relative to the theme directory unless absolute.  */
static grub_err_t
replace_path (struct theme *theme, char **dst, const char *value)
{
  char *path;

  if (value[0] == '/' || value[0] == '(')
    return replace_string (dst, value);
  path = grub_xasprintf ("%s/%s", theme->dir, value);
  if (path == NULL)
    return grub_errno;
  grub_free (*dst);
  *dst = path;
  return GRUB_ERR_NONE;
}

static grub_err_t
set_element_property (struct theme *theme, struct element *e,
		      const char *name, const char *value)
{
  grub_err_t err;
  int v;

  if (grub_strcmp (name, "id") == 0)
    return GRUB_ERR_NONE;
  if (grub_strcmp (name, "left") == 0)
    return parse_coord (value, &e->left);
  if (grub_strcmp (name, "top") == 0)
    return parse_coord (value, &e->top);
  if (grub_strcmp (name, "width") == 0)
    return parse_coord (value, &e->width);
  if (grub_strcmp (name, "height") == 0)
    return parse_coord (value, &e->height);
  if (grub_strcmp (name, "show") == 0)
    return parse_show (value, &e->show);
  if (grub_strcmp (name, "text") == 0)
    return replace_string (&e->text, value);
  if (grub_strcmp (name, "file") == 0)
    return replace_path (theme, &e->file, value);
  if (grub_strcmp (name, "font") == 0)
    return replace_string (&e->font_name, value);
  if (grub_strcmp (name, "mask") == 0)
    return replace_string (&e->mask, value);
  if (grub_strcmp (name, "placeholder") == 0)
    return replace_string (&e->placeholder, value);
  if (grub_strcmp (name, "color") == 0)
    return grub_video_parse_color (value, &e->color);
  if (grub_strcmp (name, "background-color") == 0)
    return grub_video_parse_color (value, &e->background_color);
  if (grub_strcmp (name, "border-color") == 0)
    return grub_video_parse_color (value, &e->border_color);
  if (grub_strcmp (name, "placeholder-color") == 0)
    return grub_video_parse_color (value, &e->placeholder_color);
  if (grub_strcmp (name, "cursor-color") == 0)
    return grub_video_parse_color (value, &e->cursor_color);
  if (grub_strcmp (name, "border-width") == 0)
    return parse_number (value, &e->border_width);
  if (grub_strcmp (name, "padding") == 0)
    return parse_number (value, &e->padding);
  if (grub_strcmp (name, "fixed-length") == 0)
    return parse_number (value, &e->fixed_length);
  if (grub_strcmp (name, "align") == 0)
    {
      err = parse_keyword (aligns, "align", value, &v);
      if (err == GRUB_ERR_NONE)
	e->align = v;
      return err;
    }
  if (grub_strcmp (name, "mask-mode") == 0)
    {
      err = parse_keyword (mask_modes, "mask-mode", value, &v);
      if (err == GRUB_ERR_NONE)
	e->mask_mode = v;
      return err;
    }
  return grub_error (GRUB_ERR_BAD_ARGUMENT, N_("unknown property `%s'"), name);
}

static grub_err_t
set_theme_property (struct theme *theme, const char *name, const char *value)
{
  if (grub_strcmp (name, "background-image") == 0)
    return replace_path (theme, &theme->background_image, value);
  if (grub_strcmp (name, "background-color") == 0)
    return grub_video_parse_color (value, &theme->background_color);
  if (grub_strcmp (name, "gfxmode") == 0)
    return replace_string (&theme->gfxmode, value);
  if (grub_strcmp (name, "max-tries") == 0)
    {
      int n;

      if (parse_number (value, &n) != GRUB_ERR_NONE)
	return grub_errno;
      theme->max_tries = n;
      return GRUB_ERR_NONE;
    }
  if (grub_strcmp (name, "font-file") == 0)
    {
      char *path = NULL;
      grub_font_t font;

      if (replace_path (theme, &path, value) != GRUB_ERR_NONE)
	return grub_errno;
      font = grub_font_load (path);
      grub_free (path);
      return font ? GRUB_ERR_NONE : grub_errno;
    }
  if (grub_strcmp (name, "background-mode") == 0)
    {
      int v;
      grub_err_t err = parse_keyword (background_modes, "background-mode",
				      value, &v);

      if (err == GRUB_ERR_NONE)
	theme->background_mode = v;
      return err;
    }
  return grub_error (GRUB_ERR_BAD_ARGUMENT, N_("unknown property `%s'"), name);
}

static struct element *
add_element (struct theme *theme, const char *kind)
{
  struct element *e;
  int v;

  if (parse_keyword (element_kinds, "element", kind, &v) != GRUB_ERR_NONE)
    return NULL;
  e = grub_zalloc (sizeof (*e));
  if (e == NULL)
    return NULL;
  e->kind = v;

  e->show = STATE_ALL;
  e->color = (e->kind == ELEMENT_BOX) ? rgba (0, 0, 0, 128)
				      : rgba (255, 255, 255, 255);
  e->background_color = rgba (0, 0, 0, 0);
  e->border_color = rgba (255, 255, 255, 255);
  e->placeholder_color = rgba (136, 136, 136, 255);
  e->cursor_color = rgba (255, 255, 255, 255);
  e->align = (e->kind == ELEMENT_PASSPHRASE) ? ALIGN_CENTER : ALIGN_LEFT;
  e->fixed_length = 8;
  e->padding = (e->kind == ELEMENT_PASSPHRASE) ? 8 : 0;

  *theme->tail = e;
  theme->tail = &e->next;
  return e;
}

static grub_err_t
expect (struct parser *ps, char c)
{
  char msg[] = "`?' expected";

  skip_space (ps);
  if (*ps->p != c)
    {
      msg[1] = c;
      return parse_error (ps, msg);
    }
  ps->p++;
  return GRUB_ERR_NONE;
}

/* Reads "NAME SEP VALUE" and applies it to ELEMENT, or to the theme if
   ELEMENT is NULL.  */
static grub_err_t
parse_property (struct parser *ps, struct theme *theme,
		struct element *element, char sep)
{
  char *name, *value = NULL;
  grub_err_t err;

  name = read_name (ps);
  if (name == NULL)
    return grub_errno;
  err = expect (ps, sep);
  if (err == GRUB_ERR_NONE)
    {
      skip_space (ps);
      value = read_value (ps);
      if (value == NULL)
	err = grub_errno;
    }
  if (err == GRUB_ERR_NONE)
    {
      if (element)
	err = set_element_property (theme, element, name, value);
      else
	err = set_theme_property (theme, name, value);
      if (err != GRUB_ERR_NONE)
	err = annotate_error (ps);
    }
  grub_free (name);
  grub_free (value);
  return err;
}

static grub_err_t
parse_theme (struct parser *ps, struct theme *theme)
{
  for (;;)
    {
      skip_space (ps);
      if (*ps->p == '\0')
	return GRUB_ERR_NONE;

      if (*ps->p != '+')
	{
	  if (parse_property (ps, theme, NULL, ':') != GRUB_ERR_NONE)
	    return grub_errno;
	  continue;
	}

      ps->p++;
      skip_space (ps);
      {
	char *kind = read_name (ps);
	struct element *e;

	if (kind == NULL)
	  return grub_errno;
	e = add_element (theme, kind);
	grub_free (kind);
	if (e == NULL)
	  return annotate_error (ps);
	if (expect (ps, '{') != GRUB_ERR_NONE)
	  return grub_errno;
	for (;;)
	  {
	    skip_space (ps);
	    if (*ps->p == '}')
	      break;
	    if (*ps->p == '\0')
	      return parse_error (ps, N_("`}' expected"));
	    if (parse_property (ps, theme, e, '=') != GRUB_ERR_NONE)
	      return grub_errno;
	  }
	ps->p++;
      }
    }
}

static void
free_theme (struct theme *theme)
{
  struct element *e, *next;

  if (theme == NULL)
    return;
  for (e = theme->elements; e; e = next)
    {
      next = e->next;
      grub_free (e->text);
      grub_free (e->file);
      grub_free (e->font_name);
      grub_free (e->mask);
      grub_free (e->placeholder);
      if (e->bitmap)
	grub_video_bitmap_destroy (e->bitmap);
      grub_free (e);
    }
  if (theme->background)
    grub_video_bitmap_destroy (theme->background);
  grub_free (theme->dir);
  grub_free (theme->gfxmode);
  grub_free (theme->background_image);
  grub_free (theme);
}

static struct theme *
load_theme (const char *path)
{
  struct theme *theme;
  struct parser ps;
  grub_file_t file;
  grub_off_t size;
  char *buf;
  const char *slash;

  file = grub_file_open (path, GRUB_FILE_TYPE_THEME);
  if (file == NULL)
    return NULL;
  size = grub_file_size (file);
  buf = grub_malloc (size + 1);
  if (buf == NULL)
    {
      grub_file_close (file);
      return NULL;
    }
  if (grub_file_read (file, buf, size) != (grub_ssize_t) size)
    {
      grub_file_close (file);
      grub_free (buf);
      if (grub_errno == GRUB_ERR_NONE)
	grub_error (GRUB_ERR_FILE_READ_ERROR, N_("premature end of file %s"),
		    path);
      return NULL;
    }
  grub_file_close (file);
  buf[size] = '\0';

  theme = grub_zalloc (sizeof (*theme));
  if (theme == NULL)
    {
      grub_free (buf);
      return NULL;
    }
  theme->tail = &theme->elements;
  theme->background_color = rgba (0, 0, 0, 255);
  theme->background_mode = BACKGROUND_CROP;
  theme->max_tries = DEFAULT_MAX_TRIES;
  slash = grub_strrchr (path, '/');
  theme->dir = slash ? grub_strndup (path, slash - path) : grub_strdup ("");
  if (theme->dir == NULL)
    goto fail;

  ps.path = path;
  ps.start = buf;
  ps.p = buf;
  if (parse_theme (&ps, theme) != GRUB_ERR_NONE)
    goto fail;

  grub_free (buf);
  return theme;

 fail:
  grub_free (buf);
  free_theme (theme);
  return NULL;
}

/* Drawing.  */

static int
resolve (const struct coord *c, int total, int size)
{
  if (c->center)
    return (total - size) / 2 + c->offset;
  return (int) ((grub_int64_t) total * c->percent / 100) + c->offset;
}

static int
align_x (enum align align, int x, int width, int content_width)
{
  switch (align)
    {
    case ALIGN_CENTER:
      return x + (width - content_width) / 2;
    case ALIGN_RIGHT:
      return x + width - content_width;
    default:
      return x;
    }
}

/* grub_video_fill_rect does not blend, so translucent fills go through a
   temporary bitmap.  */
static void
fill_rect_rgba (grub_video_rgba_color_t c, int x, int y, int w, int h)
{
  struct grub_video_bitmap *bitmap;
  struct grub_video_mode_info info;
  grub_uint8_t *data, *row;
  int i, j;

  if (w <= 0 || h <= 0 || c.alpha == 0)
    return;
  if (c.alpha == 255)
    {
      grub_video_fill_rect (grub_video_map_rgba_color (c), x, y, w, h);
      return;
    }
  if (grub_video_bitmap_create (&bitmap, w, h,
				GRUB_VIDEO_BLIT_FORMAT_RGBA_8888)
      != GRUB_ERR_NONE)
    {
      grub_errno = GRUB_ERR_NONE;
      return;
    }
  grub_video_bitmap_get_mode_info (bitmap, &info);
  data = grub_video_bitmap_get_data (bitmap);
  for (j = 0; j < h; j++)
    {
      row = data + j * info.pitch;
      for (i = 0; i < w; i++)
	{
	  row[4 * i] = c.red;
	  row[4 * i + 1] = c.green;
	  row[4 * i + 2] = c.blue;
	  row[4 * i + 3] = c.alpha;
	}
    }
  grub_video_blit_bitmap (bitmap, GRUB_VIDEO_BLIT_BLEND, x, y, 0, 0, w, h);
  grub_video_bitmap_destroy (bitmap);
}

static void
draw_frame (struct element *e, grub_video_rgba_color_t fill,
	    int x, int y, int w, int h)
{
  int b = e->border_width;

  fill_rect_rgba (fill, x, y, w, h);
  if (b <= 0)
    return;
  fill_rect_rgba (e->border_color, x, y, w, b);
  fill_rect_rgba (e->border_color, x, y + h - b, w, b);
  fill_rect_rgba (e->border_color, x, y + b, b, h - 2 * b);
  fill_rect_rgba (e->border_color, x + w - b, y + b, b, h - 2 * b);
}

/* Expands {device}, {attempt}, {max_tries} and {attempts_left}.  */
static char *
expand_text (const char *text, const struct screen *s)
{
  const char *p, *end;
  char *buf, *out;
  char num[32];
  grub_size_t extra, n = 0;

  for (p = text; *p; p++)
    if (*p == '{')
      n++;
  extra = n * (grub_strlen (s->device) + sizeof (num));
  buf = grub_malloc (grub_strlen (text) + extra + 1);
  if (buf == NULL)
    return NULL;

  for (p = text, out = buf; *p; )
    {
      const char *value = NULL;
      grub_size_t len;

      end = (*p == '{') ? grub_strchr (p, '}') : NULL;
      if (end != NULL)
	{
	  len = end - p - 1;
	  if (len == 6 && grub_strncmp (p + 1, "device", len) == 0)
	    value = s->device;
	  else if (len == 7 && grub_strncmp (p + 1, "attempt", len) == 0)
	    {
	      grub_snprintf (num, sizeof (num), "%lu", s->attempt);
	      value = num;
	    }
	  else if (len == 9 && grub_strncmp (p + 1, "max_tries", len) == 0)
	    {
	      grub_snprintf (num, sizeof (num), "%lu", s->theme->max_tries);
	      value = num;
	    }
	  else if (len == 13 && grub_strncmp (p + 1, "attempts_left", len) == 0)
	    {
	      unsigned long left = 0;

	      if (s->theme->max_tries > s->attempt)
		left = s->theme->max_tries - s->attempt;
	      grub_snprintf (num, sizeof (num), "%lu", left);
	      value = num;
	    }
	}
      if (value == NULL)
	{
	  *out++ = *p++;
	  continue;
	}
      len = grub_strlen (value);
      grub_memcpy (out, value, len);
      out += len;
      p = end + 1;
    }
  *out = '\0';
  return buf;
}

static void
draw_label (struct element *e, const struct screen *s)
{
  char *text;
  int x, y, w, h, tw, fh;

  if (e->text == NULL)
    return;
  text = expand_text (e->text, s);
  if (text == NULL)
    {
      grub_errno = GRUB_ERR_NONE;
      return;
    }
  fh = grub_font_get_height (e->font);
  tw = grub_font_get_string_width (e->font, text);
  w = e->width.set ? resolve (&e->width, s->width, 0) : tw + 2 * e->padding;
  h = e->height.set ? resolve (&e->height, s->height, 0)
		    : fh + 2 * e->padding;
  x = resolve (&e->left, s->width, w);
  y = resolve (&e->top, s->height, h);

  draw_frame (e, e->background_color, x, y, w, h);
  grub_font_draw_string (text, e->font, grub_video_map_rgba_color (e->color),
			 align_x (e->align, x + e->padding, w - 2 * e->padding, tw),
			 y + (h - fh) / 2 + grub_font_get_ascent (e->font));
  grub_free (text);
}

static void
draw_passphrase (struct element *e, const struct screen *s)
{
  const char *mask = e->mask ? e->mask : "*";
  char *shown = NULL;
  grub_size_t count = 0, mask_len = grub_strlen (mask), i;
  int x, y, w, h, fh, inner_w, tw, tx, cursor_x, baseline;

  fh = grub_font_get_height (e->font);
  w = e->width.set ? resolve (&e->width, s->width, 0) : 300;
  h = e->height.set ? resolve (&e->height, s->height, 0) : fh + 2 * e->padding;
  x = resolve (&e->left, s->width, w);
  y = resolve (&e->top, s->height, h);
  inner_w = w - 2 * e->padding;
  baseline = y + (h - fh) / 2 + grub_font_get_ascent (e->font);

  draw_frame (e, e->background_color, x, y, w, h);

  if (s->pass_len == 0)
    {
      tw = 0;
      tx = align_x (e->align, x + e->padding, inner_w, 0);
      cursor_x = tx;
      if (e->placeholder && s->state != STATE_BUSY)
	{
	  int pw = grub_font_get_string_width (e->font, e->placeholder);
	  int px = align_x (e->align, x + e->padding, inner_w, pw);

	  grub_font_draw_string (e->placeholder, e->font,
				 grub_video_map_rgba_color (e->placeholder_color),
				 px, baseline);
	  /* The cursor sits in front of the placeholder.  */
	  cursor_x = px - 4;
	}
    }
  else
    {
      int mask_w = grub_font_get_string_width (e->font, mask);

      if (e->mask_mode == MASK_PER_CHAR)
	count = s->pass_len;
      else if (e->mask_mode == MASK_FIXED)
	count = e->fixed_length;
      if (mask_w > 0 && count > (grub_size_t) (inner_w / mask_w))
	count = inner_w / mask_w;

      shown = grub_malloc (count * mask_len + 1);
      if (shown == NULL)
	{
	  grub_errno = GRUB_ERR_NONE;
	  return;
	}
      for (i = 0; i < count; i++)
	grub_memcpy (shown + i * mask_len, mask, mask_len);
      shown[count * mask_len] = '\0';
      tw = grub_font_get_string_width (e->font, shown);
      tx = align_x (e->align, x + e->padding, inner_w, tw);
      cursor_x = tx + tw + 2;
      grub_font_draw_string (shown, e->font,
			     grub_video_map_rgba_color (e->color), tx, baseline);
      grub_free (shown);
    }

  if (s->cursor_on && s->state != STATE_BUSY)
    fill_rect_rgba (e->cursor_color, cursor_x, y + (h - fh) / 2, 2, fh);
}

static void
draw_screen (const struct screen *s)
{
  struct theme *theme = s->theme;
  struct element *e;
  grub_video_rgba_color_t bg = theme->background_color;

  bg.alpha = 255;
  grub_video_fill_rect (grub_video_map_rgba_color (bg), 0, 0,
			s->width, s->height);
  if (theme->background)
    {
      int bw = grub_video_bitmap_get_width (theme->background);
      int bh = grub_video_bitmap_get_height (theme->background);

      grub_video_blit_bitmap (theme->background, GRUB_VIDEO_BLIT_BLEND,
			      ((int) s->width - bw) / 2,
			      ((int) s->height - bh) / 2, 0, 0, bw, bh);
    }

  for (e = theme->elements; e; e = e->next)
    {
      if (!(e->show & s->state))
	continue;
      switch (e->kind)
	{
	case ELEMENT_BOX:
	  {
	    int w = resolve (&e->width, s->width, 0);
	    int h = resolve (&e->height, s->height, 0);

	    draw_frame (e, e->color, resolve (&e->left, s->width, w),
			resolve (&e->top, s->height, h), w, h);
	  }
	  break;
	case ELEMENT_IMAGE:
	  if (e->bitmap)
	    {
	      int w = grub_video_bitmap_get_width (e->bitmap);
	      int h = grub_video_bitmap_get_height (e->bitmap);

	      grub_video_blit_bitmap (e->bitmap, GRUB_VIDEO_BLIT_BLEND,
				      resolve (&e->left, s->width, w),
				      resolve (&e->top, s->height, h),
				      0, 0, w, h);
	    }
	  break;
	case ELEMENT_LABEL:
	  draw_label (e, s);
	  break;
	case ELEMENT_PASSPHRASE:
	  draw_passphrase (e, s);
	  break;
	}
    }

  grub_video_swap_buffers ();
  grub_errno = GRUB_ERR_NONE;
}

/* Loads bitmaps and fonts once the screen size is known.  */
static grub_err_t
prepare_theme (struct theme *theme, unsigned int width, unsigned int height)
{
  struct element *e;

  if (theme->background_image)
    {
      struct grub_video_bitmap *raw, *scaled = NULL;
      int bw, bh;

      if (grub_video_bitmap_load (&raw, theme->background_image)
	  != GRUB_ERR_NONE)
	return grub_errno;
      bw = grub_video_bitmap_get_width (raw);
      bh = grub_video_bitmap_get_height (raw);
      if (theme->background_mode == BACKGROUND_CENTER
	  || ((unsigned int) bw == width && (unsigned int) bh == height))
	theme->background = raw;
      else
	{
	  if (theme->background_mode == BACKGROUND_STRETCH)
	    grub_video_bitmap_create_scaled (&scaled, width, height, raw,
					     GRUB_VIDEO_BITMAP_SCALE_METHOD_BEST);
	  else
	    grub_video_bitmap_scale_proportional
	      (&scaled, width, height, raw,
	       GRUB_VIDEO_BITMAP_SCALE_METHOD_BEST,
	       theme->background_mode == BACKGROUND_CROP
	       ? GRUB_VIDEO_BITMAP_SELECTION_METHOD_CROP
	       : GRUB_VIDEO_BITMAP_SELECTION_METHOD_PADDING,
	       GRUB_VIDEO_BITMAP_V_ALIGN_CENTER,
	       GRUB_VIDEO_BITMAP_H_ALIGN_CENTER);
	  grub_video_bitmap_destroy (raw);
	  if (grub_errno != GRUB_ERR_NONE)
	    return grub_errno;
	  theme->background = scaled;
	}
    }

  for (e = theme->elements; e; e = e->next)
    {
      if (e->kind == ELEMENT_LABEL || e->kind == ELEMENT_PASSPHRASE)
	{
	  e->font = grub_font_get (e->font_name ? e->font_name : "");
	  /* grub_font_get silently falls back to another font.  */
	  if (e->font_name
	      && grub_strcmp (grub_font_get_name (e->font), e->font_name) != 0)
	    return grub_error (GRUB_ERR_BAD_FONT,
			       N_("font `%s' is not loaded (check font-file)"),
			       e->font_name);
	}

      if (e->kind != ELEMENT_IMAGE || e->file == NULL)
	continue;
      if (grub_video_bitmap_load (&e->bitmap, e->file) != GRUB_ERR_NONE)
	return grub_errno;
      if (e->width.set || e->height.set)
	{
	  struct grub_video_bitmap *scaled;
	  int bw = grub_video_bitmap_get_width (e->bitmap);
	  int bh = grub_video_bitmap_get_height (e->bitmap);
	  int w = e->width.set ? resolve (&e->width, width, 0) : 0;
	  int h = e->height.set ? resolve (&e->height, height, 0) : 0;

	  /* Keep the aspect ratio when only one side is given.  */
	  if (!e->width.set)
	    w = bw * h / bh;
	  if (!e->height.set)
	    h = bh * w / bw;
	  if (w <= 0 || h <= 0)
	    return grub_error (GRUB_ERR_BAD_ARGUMENT,
			       N_("invalid size for image `%s'"), e->file);
	  if (grub_video_bitmap_create_scaled (&scaled, w, h, e->bitmap,
					       GRUB_VIDEO_BITMAP_SCALE_METHOD_BEST)
	      != GRUB_ERR_NONE)
	    return grub_errno;
	  grub_video_bitmap_destroy (e->bitmap);
	  e->bitmap = scaled;
	}
    }
  return GRUB_ERR_NONE;
}

/* Input.  */

static int
read_key (struct screen *s)
{
  grub_uint64_t next = grub_get_time_ms () + CURSOR_BLINK_MS;

  for (;;)
    {
      int key = grub_getkey_noblock ();

      if (key != GRUB_TERM_NO_KEY)
	{
	  s->cursor_on = 1;
	  return key;
	}
      if (grub_get_time_ms () >= next)
	{
	  s->cursor_on = !s->cursor_on;
	  draw_screen (s);
	  next = grub_get_time_ms () + CURSOR_BLINK_MS;
	}
      grub_millisleep (10);
    }
}

/* Returns 0 if the user gave up (Esc on an empty field).  */
static int
read_passphrase (struct screen *s, char *buf, grub_size_t size)
{
  s->pass_len = 0;
  s->cursor_on = 1;
  for (;;)
    {
      int key;

      draw_screen (s);
      key = read_key (s);
      if (key == '\n' || key == '\r')
	{
	  buf[s->pass_len] = '\0';
	  return 1;
	}
      if (key == GRUB_TERM_ESC)
	{
	  if (s->pass_len == 0)
	    return 0;
	  grub_memset (buf, 0, size);
	  s->pass_len = 0;
	  continue;
	}
      if (key == '\b')
	{
	  if (s->pass_len)
	    buf[--s->pass_len] = '\0';
	  continue;
	}
      if (!grub_isprint (key))
	continue;
      if (s->pass_len + 1 < size)
	buf[s->pass_len++] = key;
    }
}

/* The real cryptomount, which our override shadows.  */
static grub_command_t real_cryptomount;

/* The passphrase is read directly behind this prefix, so the argument for
   cryptomount needs no copy of it (GRUB's argument parser does not copy
   either), and wiping that one buffer wipes every copy we made.  */
#define PASSWORD_OPTION		"--password="
#define PASSWORD_OPTION_LEN	(sizeof (PASSWORD_OPTION) - 1)

/* PASSWORD_ARG is "--password=..." or NULL to ask the plain way.  */
static grub_err_t
cryptomount (char *device, int by_uuid, char *password_arg)
{
  char *argv[3];
  int argc = 0;

  if (password_arg)
    argv[argc++] = password_arg;
  if (by_uuid)
    argv[argc++] = (char *) "-u";
  argv[argc++] = device;
  return real_cryptomount->func (real_cryptomount, argc, argv);
}

/* Sets *FALLBACK if the themed screen could not be shown at all.  */
static grub_err_t
unlock (struct theme *theme, char *device, int by_uuid, int *fallback)
{
  struct grub_video_mode_info info;
  struct grub_term_output *outputs;
  struct screen s;
  char *password_arg, *pass;
  int mode_set = 0;
  grub_err_t err;

  if (grub_video_get_info (&info) != GRUB_ERR_NONE)
    {
      const char *modevar = theme->gfxmode;
      char *modes;

      grub_errno = GRUB_ERR_NONE;
      if (modevar == NULL)
	modevar = grub_env_get ("gfxmode");
      modes = grub_xasprintf ("%s;" DEFAULT_VIDEO_MODE,
			      modevar ? modevar : DEFAULT_VIDEO_MODE);
      if (modes == NULL)
	goto setup_failed;
      err = grub_video_set_mode (modes, GRUB_VIDEO_MODE_TYPE_PURE_TEXT, 0);
      grub_free (modes);
      if (err != GRUB_ERR_NONE)
	goto setup_failed;
      mode_set = 1;
      if (grub_video_get_info (&info) != GRUB_ERR_NONE)
	goto setup_failed;
    }

  /* gfxmenu redraws its timeout bar with clipping to the bar's area and
     leaves it enabled, so when the timeout runs out only that strip of
     the screen would be drawn.  Draw to the whole display.  */
  grub_video_set_active_render_target (GRUB_VIDEO_RENDER_TARGET_DISPLAY);
  grub_video_set_viewport (0, 0, info.width, info.height);
  grub_video_set_area_status (GRUB_VIDEO_AREA_DISABLED);
  grub_errno = GRUB_ERR_NONE;

  if (prepare_theme (theme, info.width, info.height) != GRUB_ERR_NONE)
    goto setup_failed;

  password_arg = grub_zalloc (PASSWORD_OPTION_LEN
			      + GRUB_CRYPTODISK_MAX_PASSPHRASE);
  if (password_arg == NULL)
    goto setup_failed;
  grub_memcpy (password_arg, PASSWORD_OPTION, PASSWORD_OPTION_LEN);
  pass = password_arg + PASSWORD_OPTION_LEN;

  s.theme = theme;
  s.width = info.width;
  s.height = info.height;
  s.state = STATE_PROMPT;
  s.device = device;

  /* Text output (e.g. "Attempting to decrypt master key...") would be
     drawn over the screen, so mute all terminals while it is shown.  */
  outputs = grub_term_outputs;
  grub_term_outputs = NULL;

  for (s.attempt = 1; ; s.attempt++)
    {
      if (!read_passphrase (&s, pass, GRUB_CRYPTODISK_MAX_PASSPHRASE))
	{
	  err = grub_error (GRUB_ERR_BAD_ARGUMENT, "passphrase not supplied");
	  break;
	}
      s.state = STATE_BUSY;
      draw_screen (&s);
      err = cryptomount (device, by_uuid, password_arg);
      grub_memset (pass, 0, GRUB_CRYPTODISK_MAX_PASSPHRASE);
      s.pass_len = 0;
      if (err != GRUB_ERR_ACCESS_DENIED
	  || (theme->max_tries && s.attempt >= theme->max_tries))
	break;
      grub_errno = GRUB_ERR_NONE;
      s.state = STATE_ERROR;
    }
  /* Wipe again before freeing, whichever way the loop ended.  */
  grub_memset (pass, 0, GRUB_CRYPTODISK_MAX_PASSPHRASE);
  grub_free (password_arg);

  grub_term_outputs = outputs;
  if (mode_set)
    grub_video_restore ();
  else
    grub_cls ();
  grub_errno = err;
  return err;

 setup_failed:
  if (mode_set)
    grub_video_restore ();
  *fallback = 1;
  return grub_errno;
}

static const struct grub_arg_option options[] =
  {
    {"theme", 't', 0, N_("Theme file. Default: $unlock_theme."),
     N_("FILE"), ARG_TYPE_STRING},
    {"uuid", 'u', 0, N_("Mount by UUID."), 0, 0},
    {0, 0, 0, 0, 0, 0}
  };

enum
  {
    OPTION_THEME,
    OPTION_UUID
  };

static grub_err_t
unlock_with_theme (const char *path, char *device, int by_uuid)
{
  struct theme *theme = NULL;
  int fallback = 0;
  grub_err_t err;

  if (by_uuid && grub_cryptodisk_get_by_uuid (device))
    return GRUB_ERR_NONE;

  if (path == NULL)
    grub_error (GRUB_ERR_BAD_ARGUMENT, N_("no theme given"));
  else
    theme = load_theme (path);

  if (theme)
    {
      err = unlock (theme, device, by_uuid, &fallback);
      free_theme (theme);
      if (!fallback)
	return err;
    }

  /* Show why the theme failed, then ask the plain way.  */
  grub_print_error ();
  return cryptomount (device, by_uuid, NULL);
}

static grub_err_t
grub_cmd_unlock_theme (grub_extcmd_context_t ctxt, int argc, char **args)
{
  struct grub_arg_list *state = ctxt->state;

  if (argc != 1)
    return grub_error (GRUB_ERR_BAD_ARGUMENT, N_("one argument expected"));
  return unlock_with_theme (state[OPTION_THEME].set ? state[OPTION_THEME].arg
			    : grub_env_get ("unlock_theme"),
			    args[0], state[OPTION_UUID].set);
}

/* With $unlock_theme set, plain "cryptomount -u UUID" and
   "cryptomount DEVICE" show the themed screen, so existing menu entries
   need no changes.  Anything else goes to the real cryptomount.  */
static grub_err_t
grub_cmd_cryptomount_themed (grub_command_t c __attribute__ ((unused)),
			     int argc, char **args)
{
  const char *path = grub_env_get ("unlock_theme");
  int by_uuid = (argc == 2 && grub_strcmp (args[0], "-u") == 0);

  if (path && *path && (by_uuid || (argc == 1 && args[0][0] != '-')))
    return unlock_with_theme (path, args[argc - 1], by_uuid);
  return real_cryptomount->func (real_cryptomount, argc, args);
}

static grub_extcmd_t cmd;
static grub_command_t cryptomount_override;

GRUB_MOD_INIT (unlock_theme)
{
  cmd = grub_register_extcmd ("unlock_theme", grub_cmd_unlock_theme, 0,
			      N_("[-t FILE] [-u] SOURCE|UUID"),
			      N_("Unlock an encrypted device using a themed "
				 "passphrase screen."),
			      options);
  /* cryptodisk is a dependency, so its cryptomount is registered.  */
  real_cryptomount = grub_command_find ("cryptomount");
  if (real_cryptomount)
    cryptomount_override = grub_register_command_prio ("cryptomount",
						       grub_cmd_cryptomount_themed,
						       real_cryptomount->summary,
						       real_cryptomount->description,
						       1);
}

GRUB_MOD_FINI (unlock_theme)
{
  if (cryptomount_override)
    grub_unregister_command (cryptomount_override);
  grub_unregister_extcmd (cmd);
}
