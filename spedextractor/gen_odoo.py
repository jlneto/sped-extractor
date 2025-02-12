import logging
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Dict, List

import click
from black import FileMode, format_str
from xsdata.codegen.models import Attr, AttrType, Class, Restrictions
from xsdata.models.config import GeneratorConfig, GeneratorSubstitution, ObjectType
from xsdata_odoo.generator import OdooFilters, OdooGenerator
from xsdata_odoo.text_utils import extract_string_and_help

from .build_csv import get_fields, get_registers
from .constants import MODULES, MOST_RECENT_YEAR, OLDEST_YEAR, SPECS_PATH
from .download import get_version

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

HEADER = """# Copyright 2023 - TODAY, Akretion - Raphael Valyi <raphael.valyi@akretion.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.en.html).
# Generated by https://github.com/akretion/sped-extractor and xsdata-odoo
# flake8: noqa: B950
"""

IMPORTS = """import textwrap

from odoo import fields, models
"""


def collect_register_children(registers):
    """read the registers hierarchy."""
    for register_info in registers:
        if register_info["level"] > 1:
            collect_children = False
            children_o2m = []
            children_m2o = []
            level = register_info["level"]
            for r in registers:
                if r["code"] == register_info["code"]:
                    collect_children = True
                    continue
                if collect_children:
                    if r["level"] == level + 1:
                        if r["card"].strip() == "1:1" or r["card"].strip() == "1;1":
                            children_m2o.append(r)
                        else:
                            children_o2m.append(r)
                            r["o2m_parent"] = register_info
                        r["parent"] = register_info
                    elif r["level"] <= level:
                        break
            register_info["children_o2m"] = children_o2m
            register_info["children_m2o"] = children_m2o


def _get_alphanum_sequence(register_code):
    """
    Used to order the SPED register in the same order
    as in the SPED layout (the register name alone won't cut it)
    """
    bloco_key = register_code[0]
    if bloco_key == "0":
        return "0" + register_code
    elif bloco_key == "1":
        return "2" + register_code
    elif bloco_key == "9":
        return "3" + register_code
    else:
        return "1" + register_code


def get_structure(mod, registers):
    structure = f"STRUCTURE SPED {mod.upper()}"
    for reg in registers:
        short_desc, left = extract_string_and_help(
            mod, reg["code"], reg["desc"], set(), 100
        )
        reg["short_desc"] = short_desc

        if reg["level"] == 0:
            continue
        if reg["level"] == 1:
            if "990" not in reg["code"] and "099" not in reg["code"]:  # not enceramento
                structure += "\n\n<BLOCO " + reg["code"][0] + ">"
            continue
        if reg["level"] == 2:
            structure += "\n"
            desc = reg["short_desc"].upper()
        elif reg["level"] == 3:
            desc = reg["short_desc"]
        else:
            desc = ""
        if desc == reg["code"]:
            desc = reg["desc"][:40] + "..."
        if reg.get("o2m_parent"):
            structure += (
                "\n"
                + "  " * (reg["level"] - 1)
                + "\u2261 "
                + (reg["code"] + " " + desc).strip()
            )
        else:
            structure += (
                "\n"
                + "  " * (reg["level"] - 1)
                + "- "
                + (reg["code"] + " " + desc).strip()
            )
    return structure


class SpedFilters(OdooFilters):
    def registry_name(
        self, name: str = "", parents: List[Class] = [], type_names: List[str] = []
    ) -> str:
        name = self.class_name(name)
        return f"{self.schema}.{self.version}.{name[-4:].lower()}"

    def registry_comodel(self, type_names: List[str]):
        # NOTE: we take only the last part of inner Types with .split(".")[-1]
        # but if that were to create Type duplicates we could change that.
        clean_type_names = type_names[-1].replace('"', "").split(".")
        comodel = self.registry_name(clean_type_names[-1], type_names=clean_type_names)
        comodel = ".".join(comodel.split(".")[0:2] + comodel.split(".")[-1:])
        return comodel

    def class_properties(
        self,
        obj: Class,
        parents: List[Class],
    ) -> str:
        register = list(filter(lambda x: x["code"] == obj.name[-4:], self.registers))[0]
        return f"_sped_level = {register['level']}"

    def odoo_class_name(self, obj: Class, parents: List[Class] = []):
        return obj.name

    def odoo_inherit_model(self, obj: Class) -> str:
        if "0000" in obj.name:
            return "l10n_br_sped.declaration"
        else:
            return self.inherit_model

    def _extract_field_attributes(self, parents: List[Class], attr: Attr):
        """
        xsdata-odoo override. Note that because we pass native xsdata types
        (Model, Attr, Restriction...) to xsdata-odoo, we may not be able to
        pass every detail we want to the templating. So here we lookup for
        these details again when we need.
        """
        obj = parents[-1]
        kwargs = OrderedDict()
        if not hasattr(obj, "unique_labels"):
            obj.unique_labels = set()  # will avoid repeating field labels
        string, help_attr = extract_string_and_help(
            obj.name, attr.name, attr.help, obj.unique_labels, 50
        )
        if string.endswith("_ids"):
            # no short string was extracted -> string = register code
            string = string.split("_")[1]
        kwargs["string"] = string

        metadata = self.field_metadata(attr, {}, [p.name for p in parents])
        if metadata.get("required") or (not attr.is_list and not attr.is_optional):
            kwargs["required"] = True

        # relational kwargs:
        if attr.name.endswith("_id") and "_Registro" in attr.name:
            kwargs["ondelete"] = "cascade"
        elif attr.name.startswith("reg_") and attr.name.endswith("_ids"):
            target_reg_code = attr.name.replace("reg_", "").replace("_ids", "")
            target_register = list(
                filter(lambda x: x["code"] == target_reg_code, self.registers)
            )[0]
            kwargs["sped_card"] = target_register["card"]
            if target_register.get("spec_required") == "Sim":
                kwargs["sped_required"] = True

        else:
            # simple types
            field = list(
                filter(
                    lambda x: x["code"] == attr.name and x["register"] == obj.name[-4:],
                    self.fields,
                )
            )[0]

            if field.get("xsd_type"):
                kwargs["xsd_type"] = field["xsd_type"]

            if field.get("in_required"):
                kwargs["in_required"] = True
            elif field.get("out_required"):
                kwargs["out_required"] = True

            if field.get(
                "length"
            ):  # as str because ometimes we have an '*' in the pdfs -> means more than
                kwargs["sped_length"] = str(field["length"].replace("0", ""))
            if (
                attr.types
                and attr.types[0].datatype.code == "float"
                and field.get("decimal")
            ):
                digits = int(field["decimal"])
                if digits < 10:
                    kwargs["xsd_type"] = f"TDec_160{digits}"
                else:
                    kwargs["xsd_type"] = f"TDec_16{digits}"

        if help_attr:
            kwargs["help"] = help_attr

        return kwargs

    def _extract_number_attrs(self, obj: Class, attr: Attr, kwargs: Dict[str, Dict]):
        python_type = attr.types[0].datatype.code
        if python_type in ("float", "decimal", "integer"):
            xsd_type = kwargs.get("xsd_type", "")

            # Brazilian fiscal documents:
            if xsd_type.startswith("TDec_"):
                if int(xsd_type[7:9]) != 2 or (
                    not attr.name.startswith("VL_")
                    and not attr.name.startswith("VAL_")
                    and not attr.name.startswith("VALOR")
                ):
                    kwargs["digits"] = (
                        int(xsd_type[5:7]),
                        int(xsd_type[7:9]),
                    )
                else:
                    kwargs[
                        "currency_field"
                    ] = "brl_currency_id"  # use company_curreny_id?
            elif attr.name.startswith(("VL_", "VAL_", "VALOR")):
                kwargs["currency_field"] = "brl_currency_id"  # use company_curreny_id?


@click.option(
    "--year",
    default=MOST_RECENT_YEAR,
    show_default=True,
    type=click.IntRange(OLDEST_YEAR, MOST_RECENT_YEAR),
    help="Operate on a specific year's folder, "
    f"can be between {OLDEST_YEAR} and {MOST_RECENT_YEAR}",
)
@click.command()
def main(year):
    """Generate Odoo models."""

    config = GeneratorConfig()
    config.conventions.field_name.safe_prefix = (
        "NO_PREFIX_NO_SAFE_NAME"  # no field prefix
    )
    generator = OdooGenerator(config)

    generator.filters = SpedFilters(
        config,
        [],
        [],
        {},  # registry_names
        defaultdict(list),
    )
    generator.filters.register(generator.env)
    generator.filters.python_inherit_model = "models.AbstractModel"

    for mod in MODULES:
        print(f"\n\n********************* Generating {mod} *********************")
        schema = f"l10n_br_sped.{mod}"
        generator.filters.inherit_model = f"l10n_br_sped.mixin.{mod}"
        generator.filters.schema = schema
        version = get_version(mod, year)
        generator.filters.version = version
        mod_fields = get_fields(mod, year)

        security_csv = f""""id","name","model_id:id","group_id:id","perm_read","perm_write","perm_create","perm_unlink"
"""

        views_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<odoo>'
        views_xml += (
            '\n    <menuitem name="%s"'
            ' parent="l10n_br_sped_base.menu_root" id="%s" sequence="2" />'
        ) % (mod.replace("_", " ").upper(), mod)

        action = """\n
    <record id="declaration_%s_action" model="ir.actions.act_window">
    <field name="name">%s Declaration</field>
    <field name="res_model">l10n_br_sped.%s.0000</field>
    <field name="view_mode">tree,form</field>
    </record>""" % (
            mod,
            mod.upper(),
            mod,
        )
        views_xml += action

        views_xml += (
            '\n    <menuitem action="declaration_%s_action"'
            ' parent="%s" id="declaration_%s" />'
        ) % (
            mod,
            mod,
            mod,
        )

        concrete_models_source = (
            HEADER + "\nimport textwrap\n\nfrom odoo import models\n"
        )

        last_bloco = None

        classes = []
        registers = list(
            sorted(
                filter(
                    lambda x: x["code"][0] != "C"
                    or mod not in ("ecd", "ecf"),  # filled by the validator
                    get_registers(mod, year),
                ),
                key=lambda x: _get_alphanum_sequence(x["code"]),
            )
        )
        collect_register_children(registers)
        generator.filters.registers = registers
        generator.filters.fields = mod_fields

        for register in registers:
            if register["level"] in (0, 1) and register["code"] != "0000":
                # Blocks and their start/end registers don't need to be in the database
                continue

            if register["code"][0] == "9":
                # bloco 9 is automatic, not ERP data
                break

            short_desc, left = extract_string_and_help(
                mod, register["code"], register["desc"], set(), 100
            )
            register["short_desc"] = short_desc

            if register["level"] > 1 or register["code"] == "0000":
                concrete_models_source += (
                    f"\n\nclass Registro{register['code']}(models.Model):"
                )
                concrete_models_source += f"""\n    \"{register['short_desc']}\""""
                concrete_models_source += (
                    f"""\n    _description = textwrap.dedent("    %s" % (__doc__,))"""
                )
                concrete_models_source += f"""\n    _name = \"l10n_br_sped.{mod}.{register['code'].lower()}\""""
                concrete_models_source += f"""\n    _inherit = \"l10n_br_sped.{mod}.{version}.{register['code'].lower()}\""""
                concrete_models_source += """

    # @api.model
    # def _map_from_odoo(self, record, parent_record, declaration):
    #     return {
                """

            bloco_char = register["code"][0]
            if bloco_char != last_bloco:
                views_xml += (
                    '\n\n\n    <menuitem name="BLOCO %s"' ' parent="%s" id="%s_%s" />'
                ) % (bloco_char, mod, mod, bloco_char.lower())
            last_bloco = bloco_char

            if register["level"] == 2:# or register["code"] == "0000":
                action_name = register["code"] + " " + register["short_desc"]
                action = """\n
    <record id="%s_%s_action" model="ir.actions.act_window">
        <field name="name">%s</field>
        <field name="res_model">l10n_br_sped.%s.%s</field>
        <field name="view_mode">tree,form</field>
    </record>""" % (
                    mod,
                    register["code"].lower(),
                    action_name,
                    mod,
                    register["code"].lower(),
                )
                views_xml += action

                views_xml += (
                    '\n    <menuitem action="%s_%s_action"'
                    ' parent="%s_%s" id="%s_%s" />'
                ) % (
                    mod,
                    register["code"].lower(),
                    mod,
                    bloco_char.lower(),
                    mod,
                    register["code"].lower(),
                )

            name = f"Registro{register['code']}"
            attrs = []

            # 1st we will make the register fields code unique. Incredibly in the SPED
            # spec pdfs some register have duplicate field codes.
            # example: EFD ICMS/IPI C170 with PIS_ALIQ or COFINS_ALIQ
            # one is the percent and another is the R$ value...
            # other case in EFD PIS/COFINS M210.
            # in this case we append the line field index to the dup codes
            # the field name doesn't really matter as it is not written in the SPED file.
            unique_codes = set()
            fields = []
            for field in list(
                filter(lambda x: x["register"] == register["code"], mod_fields)
            ):
                if field["code"] not in unique_codes:
                    unique_codes.add(field["code"])
                else:
                    field["code"] = f'{field["code"]}_INDEX_{field["index"]}'
                    unique_codes.add(field["code"])
                fields.append(field)

            for field in fields:
                if field["code"] in ("REG",):  # no need for DB field for fixed field
                    continue
                if not field.get("type"):
                    field["type"] = "char"

                # listing all fields helps writting and reviewing mappings:
                max_desc = 88 - len(field["code"]) - 29
                concrete_models_source += (
                    f"""    #         "{field["code"]}": 0,  # {field["desc"][0:max_desc]}{len(field["desc"]) > max_desc and "..." or ""}\n"""
                )

                if (
                    field["code"].startswith("DT_")
                    or field["code"].startswith("DAT_")
                    or field["code"].startswith("DATA")
                ):
                    types = [
                        AttrType(
                            qname="{http://www.w3.org/2001/XMLSchema}date", native=True
                        )
                    ]
                elif (
                    field["type"] == "int"
                    or field["type"] == "float"
                    and field.get("decimal")
                    and int(field["decimal"]) == 0
                ):
                    types = [
                        AttrType(
                            qname="{http://www.w3.org/2001/XMLSchema}integer",
                            native=True,
                        )
                    ]
                elif field["type"] == "float":
                    types = [
                        AttrType(
                            qname="{http://www.w3.org/2001/XMLSchema}float", native=True
                        )
                    ]
                else:
                    types = [
                        AttrType(
                            qname="{http://www.w3.org/2001/XMLSchema}string",
                            native=True,
                        )
                    ]
                    # TODO Some string fields are in fact Selection fields!
                # TODO diff entrada/saida e O / OC (Obrigatorio Condicional); see ICMS C170
                restrictions = Restrictions(
                    min_occurs=field.get("required")
                    and 1
                    or 0  # TODO if required 'OC' -> no
                )
                attr = Attr(
                    tag=field["code"],
                    name=field["code"],
                    types=types,
                    restrictions=restrictions,
                    help=field["desc"],
                    index=field["index"],
                )
                attrs.append(attr)

            # TODO if register spec_in or spec_out, then add a register_type = Field.Selection(["in", "out"])
            # only if level = 2?

            #            if register["level"] == 2:
            #                dates = list(filter(lambda x: "date" in x.types[0].qname, attrs))
            #                if len(dates) == 1:
            #                    print("DATE ", register["code"], dates[0].name)
            #                    print("NO DATE IN", register["code"], [attr.name for attr in attrs])

            #            for child in register["children_m2o"]:
            #                child_qname = "Registro{}".format(child["code"])
            #                types = [AttrType(qname=child_qname, native=False)]
            #                m2o_field_name = "reg_{}_id".format(child["code"])
            #                restrictions = Restrictions(
            #                    min_occurs=0  # TODO sure?
            #                )
            #                attr = Attr(
            #                    tag=m2o_field_name,
            #                    name=m2o_field_name,
            #                    types=types,
            #                    restrictions=restrictions,
            #                    help=child["code"] + ": " + child["desc"],
            #                )
            #                attrs.append(attr)

            concrete_models_source += "    #     }"  # close fields list

            if register.get("parent"):
                parent = register["parent"]
                parent_qname = "Registro{}".format(parent["code"])
                types = [AttrType(qname=parent_qname, native=False)]
                m2o_field_name = "reg_{}_ids_{}_id".format(
                    register["code"], parent_qname
                )
                attr = Attr(
                    tag=m2o_field_name,
                    name=m2o_field_name,
                    types=types,
                    help=parent["desc"],
                )
                attrs.append(attr)

            for child in register.get("children_m2o", []) + register.get(
                "children_o2m", []
            ):
                child_qname = "Registro{}".format(child["code"])
                types = [AttrType(qname=child_qname, native=False)]
                restrictions = Restrictions(max_occurs=999999)

                o2m_field_name = "reg_{}_ids".format(child["code"])
                # TODO find a way to pass string=child["code"]
                attr = Attr(
                    tag=o2m_field_name,
                    name=o2m_field_name,
                    types=types,
                    restrictions=restrictions,
                    help=child["code"] + " " + child["desc"],
                )
                attrs.append(attr)

            # TODO patch ECD Registro0035
            k = Class(
                qname=name,
                tag=name,
                location="TODO",
                attrs=attrs,
                help=register["desc"],
                module=mod,
            )
            classes.append(k)
            generator.filters.all_complex_types.append(k)
            security_csv += f"access_user_{mod}_{register['code'].lower()},{mod}.{register['code'].lower()},model_l10n_br_sped_{mod}_{register['code'].lower()},l10n_br_fiscal.group_user,1,0,0,0\n"
            security_csv += f"access_manager_{mod}_{register['code'].lower()},{mod}.{register['code'].lower()},model_l10n_br_sped_{mod}_{register['code'].lower()},l10n_br_fiscal.group_manager,1,1,1,1\n"

        structure = get_structure(mod, registers)
        source = (
            HEADER
            + f'\n"""\n{structure}\n"""\n\n'
            + IMPORTS
            + "\n"
            + generator.render_classes(classes, None)
        )
        try:
            source = format_str(source, mode=FileMode())
            concrete_models_source = format_str(concrete_models_source, mode=FileMode())
        except Exception as e:
            print(e)

        base_path = str(SPECS_PATH) + f"/{year}/"

        path = Path(f"{base_path}/l10n_br_sped/models/sped_{mod}_spec_{version}.py")
        print("written file", path)
        path.write_text(source, encoding="utf-8")

        path = Path(f"{base_path}/l10n_br_sped/models/sped_{mod}.py")
        print("written file", path)
        path.write_text(concrete_models_source, encoding="utf-8")

        path = Path(f"{base_path}/l10n_br_sped/views/sped_{mod}.xml")
        path.write_text(views_xml + "\n</odoo>", encoding="utf-8")

        path = Path(f"{base_path}/l10n_br_sped/security/{mod}_ir.model.access.csv")
        path.write_text(security_csv, encoding="utf-8")


if __name__ == "__main__":
    main()
