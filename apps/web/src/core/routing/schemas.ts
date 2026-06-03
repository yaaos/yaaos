import { z } from "zod";

/** Validated search schema for the /tickets list page. */
export const ticketsSearchSchema = z.object({
  q: z.string().optional(),
  repo: z.string().optional(),
  status: z.array(z.string()).optional(),
  mine: z.boolean().optional(),
});

/** Validated search schema for the /lessons list page. */
export const lessonsSearchSchema = z.object({
  q: z.string().optional(),
  repo: z.string().optional(),
  sort: z.enum(["created_desc", "created_asc", "updated_desc"]).optional(),
});
