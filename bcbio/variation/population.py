"""Provide infrastructure to allow exploration of variations within populations.

Uses the gemini framework (https://github.com/arq5x/gemini) to build SQLite
database of variations for query and evaluation.
"""
import collections
import csv
import os

import toolz as tz

from bcbio import install, utils
from bcbio.bam import ref
from bcbio.distributed.transaction import file_transaction
from bcbio.pipeline import config_utils
from bcbio.pipeline import datadict as dd
from bcbio.provenance import do
from bcbio.variation import naming, normalize, vcfanno, vcfutils

# Current callers we can't create databases for
# mutect2 -- fails on multi-allelic inputs represented as non-diploid
# https://github.com/broadinstitute/gatk/issues/3564
NO_DB_CALLERS = ["mutect2"]

def prep_gemini_db(fnames, call_info, samples, extras):
    """Prepare a gemini database from VCF inputs prepared with snpEff.
    """#
    data = samples[0]
    name, caller, is_batch = call_info
    build_type = _get_build_type(fnames, samples, caller)
    out_dir = utils.safe_makedir(os.path.join(data["dirs"]["work"], "gemini"))
    gemini_vcf = get_multisample_vcf(fnames, name, caller, data)
    # If we're building a gemini database, normalize the inputs
    if build_type:
        passonly = all("gemini_allvariants" not in dd.get_tools_on(d) for d in samples)
        gemini_vcf = normalize.normalize(gemini_vcf, data, passonly=passonly)
        decomposed = True
    else:
        decomposed = False
    ann_vcf = run_vcfanno(gemini_vcf, data, decomposed)
    gemini_db = os.path.join(out_dir, "%s-%s.db" % (name, caller))
    if ann_vcf and build_type and not utils.file_exists(gemini_db):
        ped_file = create_ped_file(samples + extras, gemini_vcf)
        # Original approach for hg19/GRCh37
        if is_human(data, builds=["37"]) and "gemini_orig" in build_type:
            gemini_db = create_gemini_db_orig(gemini_vcf, data, gemini_db, ped_file)
        else:
            gemini_db = create_gemini_db(ann_vcf, data, gemini_db, ped_file)
    return [[(name, caller), {"db": gemini_db if utils.file_exists(gemini_db) else None,
                              "vcf": ann_vcf or gemini_vcf,
                              "decomposed": decomposed}]]

def run_vcfanno(gemini_vcf, data, decomposed=False):
    """Run vcfanno, providing annotations from external databases.
    """
    data_basepath = install.get_gemini_dir(data) if is_human(data, builds=["37"]) else None
    conf_files = dd.get_vcfanno(data)
    if not conf_files:
        conf_files = default_conf_files(data)
    if conf_files:
        return vcfanno.run_vcfanno(gemini_vcf, conf_files, data, data_basepath=data_basepath,
                                   decomposed=decomposed)
    else:
        return gemini_vcf

def default_conf_files(data):
    conf_files = []
    if _has_gemini_data(data):
        conf_files.append("gemini")
    if _annotate_somatic(data):
        conf_files.append("somatic")
    return conf_files

def _annotate_somatic(data):
    """Annotate somatic calls if we have cosmic data installed.
    """
    if is_human(data):
        paired = vcfutils.get_paired([data])
        if paired:
            r = dd.get_variation_resources(data)
            if os.path.exists(r.get("cosmic", "")):
                return True
    return False

def create_gemini_db(gemini_vcf, data, gemini_db=None, ped_file=None):
    """Generalized vcfanno/vcf2db workflow for loading variants into a GEMINI database.
    """
    if not gemini_db:
        gemini_db = "%s.db" % utils.splitext_plus(gemini_vcf)[0]
    if not vcfutils.vcf_has_variants(gemini_vcf):
        return None
    if not utils.file_exists(gemini_db):
        with file_transaction(data, gemini_db) as tx_gemini_db:
            vcf2db = config_utils.get_program("vcf2db.py", data)
            if "vcf2db_expand" in dd.get_tools_on(data):
                vcf2db_args = ["--expand", "gt_types", "--expand", "gt_ref_depths", "--expand", "gt_alt_depths"]
            else:
                vcf2db_args = []
            cmd = [vcf2db, gemini_vcf, ped_file, tx_gemini_db] + vcf2db_args
            do.run(cmd, "GEMINI: create database with vcf2db")
    return gemini_db

def create_gemini_db_orig(gemini_vcf, data, gemini_db=None, ped_file=None):
    """Original GEMINI specific data loader, only works with hg19/GRCh37.
    """
    if not gemini_db:
        gemini_db = "%s.db" % utils.splitext_plus(gemini_vcf)[0]
    if not utils.file_exists(gemini_db):
        if not vcfutils.vcf_has_variants(gemini_vcf):
            return None
        with file_transaction(data, gemini_db) as tx_gemini_db:
            gemini = config_utils.get_program("gemini", data["config"])
            load_opts = ""
            if "gemini_allvariants" not in dd.get_tools_on(data):
                load_opts += " --passonly"
            # For small test files, skip gene table loading which takes a long time
            if _is_small_vcf(gemini_vcf):
                load_opts += " --skip-gene-tables"
            if "/test_automated_output/" in gemini_vcf:
                load_opts += " --test-mode"
            # Skip CADD or gerp-bp if neither are loaded
            gemini_dir = install.get_gemini_dir(data)
            for skip_cmd, check_file in [("--skip-cadd", "whole_genome_SNVs.tsv.compressed.gz")]:
                if not os.path.exists(os.path.join(gemini_dir, check_file)):
                    load_opts += " %s" % skip_cmd
            # skip gerp-bp which slows down loading
            load_opts += " --skip-gerp-bp "
            num_cores = data["config"]["algorithm"].get("num_cores", 1)
            tmpdir = os.path.dirname(tx_gemini_db)
            eanns = _get_effects_flag(data)
            # Apply custom resource specifications, allowing use of alternative annotation_dir
            resources = config_utils.get_resources("gemini", data["config"])
            gemini_opts = " ".join([str(x) for x in resources["options"]]) if resources.get("options") else ""
            exports = utils.local_path_export()
            cmd = ("{exports} {gemini} {gemini_opts} load {load_opts} "
                   "-v {gemini_vcf} {eanns} --cores {num_cores} "
                   "--tempdir {tmpdir} {tx_gemini_db}")
            cmd = cmd.format(**locals())
            do.run(cmd, "Create gemini database for %s" % gemini_vcf, data)
            if ped_file:
                cmd = [gemini, "amend", "--sample", ped_file, tx_gemini_db]
                do.run(cmd, "Add PED file to gemini database", data)
    return gemini_db

def _get_effects_flag(data):
    effects_config = tz.get_in(("config", "algorithm", "effects"), data, "snpeff")
    if effects_config == "snpeff":
        return "-t snpEff"
    elif effects_config == "vep":
        return "-t VEP"
    else:
        return ""

def get_affected_status(data):
    """Retrieve the affected/unaffected status of sample.

    Uses unaffected (1), affected (2), unknown (0) coding from PED files:

    http://pngu.mgh.harvard.edu/~purcell/plink/data.shtml#ped
    """
    affected = set(["tumor", "affected", "2"])
    unaffected = set(["normal", "unaffected", "1"])
    phenotype = str(tz.get_in(["metadata", "phenotype"], data, "")).lower()
    if dd.get_svclass(data) == "control":
        return 1
    elif phenotype in affected:
        return 2
    elif phenotype in unaffected:
        return 1
    else:
        return 0

def get_gender(data):
    """Retrieve gender from metadata, codified as male/female/unknown.
    """
    g = str(dd.get_gender(data))
    if g and str(g).lower() in ["male", "m", "1"]:
        return "male"
    elif g and str(g).lower() in ["female", "f", "2"]:
        return "female"
    else:
        return "unknown"

def get_ped_info(data, samples):
    """Retrieve all PED info from metadata
    """
    family_id = tz.get_in(["metadata", "family_id"], data, None)
    if not family_id:
        family_id = _find_shared_batch(samples)
    return {
        "gender": {"male": 1, "female": 2, "unknown": 0}.get(get_gender(data)),
        "individual_id": dd.get_sample_name(data),
        "family_id": family_id,
        "maternal_id": tz.get_in(["metadata", "maternal_id"], data, -9),
        "paternal_id": tz.get_in(["metadata", "paternal_id"], data, -9),
        "affected": get_affected_status(data),
        "ethnicity": tz.get_in(["metadata", "ethnicity"], data, -9)
    }

def create_ped_file(samples, base_vcf, out_dir=None):
    """Create a GEMINI-compatible PED file, including gender, family and phenotype information.

    Checks for a specified `ped` file in metadata, and will use sample information from this file
    before reconstituting from metadata information.
    """
    out_file = "%s.ped" % utils.splitext_plus(base_vcf)[0]
    if out_dir:
        out_file = os.path.join(out_dir, os.path.basename(out_file))
    sample_ped_lines = {}
    header = ["#Family_ID", "Individual_ID", "Paternal_ID", "Maternal_ID", "Sex", "Phenotype", "Ethnicity"]
    for md_ped in list(set([x for x in [tz.get_in(["metadata", "ped"], data)
                                        for data in samples] if x is not None])):
        with open(md_ped) as in_handle:
            reader = csv.reader(in_handle, dialect="excel-tab")
            for parts in reader:
                if parts[0].startswith("#") and len(parts) > len(header):
                    header = header + parts[len(header):]
                else:
                    sample_ped_lines[parts[1]] = parts
    if not utils.file_exists(out_file):
        with file_transaction(samples[0], out_file) as tx_out_file:
            with open(tx_out_file, "w") as out_handle:
                writer = csv.writer(out_handle, dialect="excel-tab")
                writer.writerow(header)
                for data in samples:
                    ped_info = get_ped_info(data, samples)
                    sname = ped_info["individual_id"]
                    if sname in sample_ped_lines:
                        writer.writerow(sample_ped_lines[sname])
                    else:
                        writer.writerow([ped_info["family_id"], ped_info["individual_id"],
                                         ped_info["paternal_id"], ped_info["maternal_id"],
                                         ped_info["gender"], ped_info["affected"],
                                         ped_info["ethnicity"]])
    return out_file

def _find_shared_batch(samples):
    for data in samples:
        batch = tz.get_in(["metadata", "batch"], data, dd.get_sample_name(data))
        if not isinstance(batch, (list, tuple)):
            return batch

def _is_small_vcf(vcf_file):
    """Check for small VCFs which we want to analyze quicker.
    """
    count = 0
    small_thresh = 250
    with utils.open_gzipsafe(vcf_file) as in_handle:
        for line in in_handle:
            if not line.startswith("#"):
                count += 1
            if count > small_thresh:
                return False
    return True

def get_multisample_vcf(fnames, name, caller, data):
    """Retrieve a multiple sample VCF file in a standard location.

    Handles inputs with multiple repeated input files from batches.
    """
    unique_fnames = []
    for f in fnames:
        if f not in unique_fnames:
            unique_fnames.append(f)
    out_dir = utils.safe_makedir(os.path.join(data["dirs"]["work"], "gemini"))
    if len(unique_fnames) > 1:
        gemini_vcf = os.path.join(out_dir, "%s-%s.vcf.gz" % (name, caller))
        vrn_file_batch = None
        for variant in data.get("variants", []):
            if variant["variantcaller"] == caller and variant.get("vrn_file_batch"):
                vrn_file_batch = variant["vrn_file_batch"]
        if vrn_file_batch:
            utils.symlink_plus(vrn_file_batch, gemini_vcf)
            return gemini_vcf
        else:
            return vcfutils.merge_variant_files(unique_fnames, gemini_vcf, dd.get_ref_file(data),
                                                data["config"])
    else:
        gemini_vcf = os.path.join(out_dir, "%s-%s%s" % (name, caller, utils.splitext_plus(unique_fnames[0])[1]))
        utils.symlink_plus(unique_fnames[0], gemini_vcf)
        return gemini_vcf

def _has_gemini_data(data):
    """Use gemini if we installed required data for hg19, hg38.

    Other organisms don't have special data targets.
    """
    if is_human(data):
        from bcbio import install
        return "gemini" in install.get_defaults().get("datatarget", [])
    else:
        return True

def _get_build_type(fnames, samples, caller):
    """Confirm we should build a gemini database: need gemini in tools_on.

    Checks for valid conditions for running a database and gemini or gemini_orig
    configured in tools on.
    """
    build_type = set()
    if any(vcfutils.vcf_has_variants(f) for f in fnames) and caller not in NO_DB_CALLERS:
        genomes = set()
        for data in samples:
            if any([x in dd.get_tools_on(data)
                    for x in ["gemini", "gemini_orig", "gemini_allvariants", "vcf2db_expand"]]):
                if _has_gemini_data(data):
                    genomes.add(data["genome_build"])
                    build_type.add("gemini_orig" if "gemini_orig" in dd.get_tools_on(data) else "gemini")
        if len(genomes) == 1:
            return True
    return build_type

def is_human(data, builds=None):
    """Gemini original supports human build 37, search by name or extra GL contigs.
    """
    def has_build37_contigs(data):
        for contig in ref.file_contigs(dd.get_ref_file(data)):
            if contig.name.startswith("GL") or contig.name.find("_gl") >= 0:
                if contig.name in naming.GMAP["hg19"] or contig.name in naming.GMAP["GRCh37"]:
                    return True
        return False
    if not builds or "37" in builds:
        target_builds = ["hg19", "GRCh37"]
        if dd.get_genome_build(data) in target_builds:
            return True
        elif has_build37_contigs(data):
            return True
    if not builds or "38" in builds:
        target_builds = ["hg38"]
        if dd.get_genome_build(data) in target_builds:
            return True
    return False

def get_gemini_files(data):
    """Enumerate available gemini data files in a standard installation.
    """
    try:
        from gemini import annotations, config
    except ImportError:
        return {}
    return {"base": config.read_gemini_config()["annotation_dir"],
            "files": annotations.get_anno_files().values()}

def _group_by_batches(samples, check_fn):
    """Group data items into batches, providing details to retrieve results.
    """
    batch_groups = collections.defaultdict(list)
    singles = []
    out_retrieve = []
    extras = []
    for data in [x[0] for x in samples]:
        if check_fn(data):
            batch = tz.get_in(["metadata", "batch"], data)
            name = str(dd.get_sample_name(data))
            if batch:
                out_retrieve.append((str(batch), data))
            else:
                out_retrieve.append((name, data))
            for vrn in data["variants"]:
                if vrn.get("population", True):
                    if batch:
                        batch_groups[(str(batch), vrn["variantcaller"])].append((vrn["vrn_file"], data))
                    else:
                        singles.append((name, vrn["variantcaller"], data, vrn["vrn_file"]))
        else:
            extras.append(data)
    return batch_groups, singles, out_retrieve, extras

def _has_variant_calls(data):
    for vrn in data["variants"]:
        if vrn.get("vrn_file") and vcfutils.vcf_has_variants(vrn["vrn_file"]):
            return True
    return False

def prep_db_parallel(samples, parallel_fn):
    """Prepares gemini databases in parallel, handling jointly called populations.
    """
    batch_groups, singles, out_retrieve, extras = _group_by_batches(samples, _has_variant_calls)
    to_process = []
    has_batches = False
    for (name, caller), info in batch_groups.items():
        fnames = [x[0] for x in info]
        to_process.append([fnames, (str(name), caller, True), [x[1] for x in info], extras])
        has_batches = True
    for name, caller, data, fname in singles:
        to_process.append([[fname], (str(name), caller, False), [data], extras])
    output = parallel_fn("prep_gemini_db", to_process)
    out_fetch = {}
    for batch_id, out_file in output:
        out_fetch[tuple(batch_id)] = out_file
    out = []
    for batch_name, data in out_retrieve:
        out_variants = []
        for vrn in data["variants"]:
            use_population = vrn.pop("population", True)
            if use_population:
                vrn["population"] = out_fetch[(batch_name, vrn["variantcaller"])]
            out_variants.append(vrn)
        data["variants"] = out_variants
        out.append([data])
    for x in extras:
        out.append([x])
    return out
