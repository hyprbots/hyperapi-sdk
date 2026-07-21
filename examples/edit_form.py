"""Edit API quickstart — detect a form's blank fields, then fill them.

    export HYPERAPI_KEY="hk_live_..."
    python examples/edit_form.py path/to/intake_form.pdf

Walks the human-in-the-loop flow the two-call API is designed for:

    1. detect             → the schema of every blank field
    2. fill (free text)   → the model proposes a value per field
    3. fill (values)      → the corrected set is re-rendered, no model call

Step 3 is what a UI does after the user edits a proposed value. It's free —
the document is metered once, at detect.
"""

from __future__ import annotations

import sys

from hyperapi import HyperAPIClient


def main(path: str) -> None:
    with HyperAPIClient() as client:
        # 1. Detect. markdown_assist=True is slower but more precise on dense forms.
        detected = client.edit_detect(path, markdown_assist=True)
        job_id = detected["detect_job_id"]
        schema = detected["result"]["form_schema"]

        print(f"{len(schema)} fields detected:")
        for index, field in enumerate(schema):
            # A field's position in form_schema IS its fill index.
            print(f"  [{index}] {field['field_name']} ({field['type']}) — {field['description']}")

        # 2. Let one model call map free text onto the schema. The mapping comes
        #    back as `fills` so you can show it to the user before rendering.
        proposed = client.edit_fill(
            job_id,
            content="Patient is Jane Doe, female, DOB 1985-04-02, phone +1 302 405 1234",
            natural_language=True,
        )
        fills = proposed["result"]["fills"]
        print("\nproposed fills:")
        for fill in fills:
            print(f"  [{fill['index']}] {fill['value']}")

        # 3. The user corrected something. Resubmit *all* values deterministically
        #    — no model call, and not separately charged.
        corrected = {str(f["index"]): f["value"] for f in fills}
        if "0" in corrected:
            corrected["0"] = "Jane A. Doe"
        final = client.edit_fill(job_id, values=corrected)

        # Page images are short-lived presigned URLs, not bytes — write them out now.
        # (Re-polling the job with get_job() mints fresh ones if these expire.)
        print()
        for dest in client.download_pages(final, "filled_pages"):
            print(f"wrote {dest}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python examples/edit_form.py <form.pdf>")
    main(sys.argv[1])
