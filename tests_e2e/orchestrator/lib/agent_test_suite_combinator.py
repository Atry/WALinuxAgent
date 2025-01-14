# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import logging
import re
import urllib.parse

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

# E0401: Unable to import 'dataclasses_json' (import-error)
from dataclasses_json import dataclass_json  # pylint: disable=E0401

# Disable those warnings, since 'lisa' is an external, non-standard, dependency
#     E0401: Unable to import 'lisa' (import-error)
#     etc
from lisa import schema  # pylint: disable=E0401
from lisa.combinator import Combinator  # pylint: disable=E0401
from lisa.util import field_metadata  # pylint: disable=E0401

from tests_e2e.orchestrator.lib.agent_test_loader import AgentTestLoader, VmImageInfo


@dataclass_json()
@dataclass
class AgentTestSuitesCombinatorSchema(schema.Combinator):
    test_suites: str = field(
        default_factory=str, metadata=field_metadata(required=True)
    )
    cloud: str = field(
        default_factory=str, metadata=field_metadata(required=True)
    )
    location: str = field(
        default_factory=str, metadata=field_metadata(required=True)
    )
    image: str = field(
        default_factory=str, metadata=field_metadata(required=False)
    )
    vm_size: str = field(
        default_factory=str, metadata=field_metadata(required=False)
    )
    vm_name: str = field(
        default_factory=str, metadata=field_metadata(required=False)
    )


class AgentTestSuitesCombinator(Combinator):
    """
    The "agent_test_suites" combinator returns a list of variables that specify the environments (i.e. test VMs) that the agent
    test suites must be executed on:

        * c_env_name: Unique name for the environment, e.g. "0001-com-ubuntu-server-focal-20_04-lts-westus2"
        * c_marketplace_image: e.g. "Canonical UbuntuServer 18.04-LTS latest",
        * c_location: e.g. "westus2",
        * c_vm_size: e.g. "Standard_D2pls_v5"
        * c_vhd: e.g "https://rhel.blob.core.windows.net/images/RHEL_8_Standard-8.3.202006170423.vhd?se=..."
        * c_test_suites: e.g. [AgentBvt, FastTrack]

    (c_marketplace_image, c_location, c_vm_size) and vhd are mutually exclusive and define the environment (i.e. the test VM)
    in which the test will be executed. c_test_suites defines the test suites that should be executed in that
    environment.

    The 'vm_name' runbook parameter can be used to execute the test suites on an existing VM. In that case, the combinator
    generates a single item with these variables:

        * c_env_name: Name for the environment, same as vm_name
        * c_vm_name:  Name of the test VM
        * c_location: Location of the test VM e.g. "westus2",
        * c_test_suites: e.g. [AgentBvt, FastTrack]
    """
    def __init__(self, runbook: AgentTestSuitesCombinatorSchema) -> None:
        super().__init__(runbook)
        if self.runbook.cloud not in self._DEFAULT_LOCATIONS:
            raise Exception(f"Invalid cloud: {self.runbook.cloud}")

        if self.runbook.vm_name != '' and (self.runbook.image != '' or self.runbook.vm_size != ''):
            raise Exception("Invalid runbook parameters: When 'vm_name' is specified, 'image' and 'vm_size' should not be specified.")

        if self.runbook.vm_name != '':
            self._environments = self.create_environment_for_existing_vm()
        else:
            self._environments = self.create_environment_list()
        self._index = 0


    @classmethod
    def type_name(cls) -> str:
        return "agent_test_suites"

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return AgentTestSuitesCombinatorSchema

    def _next(self) -> Optional[Dict[str, Any]]:
        result: Optional[Dict[str, Any]] = None
        if self._index < len(self._environments):
            result = self._environments[self._index]
            self._index += 1
        return result

    _DEFAULT_LOCATIONS = {
        "china": "china north 2",
        "government": "usgovarizona",
        "public": "westus2"
    }

    def create_environment_for_existing_vm(self) -> List[Dict[str, Any]]:
        loader = AgentTestLoader(self.runbook.test_suites)

        environment: List[Dict[str, Any]] = [
            {
                "c_env_name": self.runbook.vm_name,
                "c_vm_name": self.runbook.vm_name,
                "c_location": self.runbook.location,
                "c_test_suites": loader.test_suites,
            }
        ]

        log: logging.Logger = logging.getLogger("lisa")
        log.info("******** Environment for existing VMs *****")
        log.info(
            "{  c_env_name: '%s', c_vm_name: '%s', c_location: '%s', c_test_suites: '%s' }",
            environment[0]['c_env_name'], environment[0]['c_vm_name'], environment[0]['c_location'], [s.name for s in environment[0]['c_test_suites']])
        log.info("***************************")

        return environment

    def create_environment_list(self) -> List[Dict[str, Any]]:
        loader = AgentTestLoader(self.runbook.test_suites)

        #
        # If the runbook provides any of 'image', 'location', or 'vm_size', those values
        # override any configuration values on the test suite.
        #
        # Check 'images' first and add them to 'runbook_images', if any
        #
        if self.runbook.image == "":
            runbook_images = []
        else:
            runbook_images = loader.images.get(self.runbook.image)
            if runbook_images is None:
                if not self._is_urn(self.runbook.image) and not self._is_vhd(self.runbook.image):
                    raise Exception(f"The 'image' parameter must be an image or image set name, a urn, or a vhd: {self.runbook.image}")
                i = VmImageInfo()
                i.urn = self.runbook.image  # Note that this could be a URN or the URI for a VHD
                i.locations = []
                i.vm_sizes = []
                runbook_images = [i]

        #
        # Now walk through all the test_suites and create a list of the environments (test VMs) that need to be created.
        #
        environment_list: List[Dict[str, Any]] = []
        shared_environments: Dict[str, Dict[str, Any]] = {}

        for suite_info in loader.test_suites:
            if len(runbook_images) > 0:
                images_info = runbook_images
            else:
                # The test suite may be referencing multiple image sets, and sets can intersect, so we need to ensure
                # we eliminate any duplicates.
                unique_images: Dict[str, str] = {}
                for image in suite_info.images:
                    for i in loader.images[image]:
                        unique_images[i] = i
                images_info = unique_images.values()

            for image in images_info:
                # The URN can actually point to a VHD if the runbook provided a VHD in the 'images' parameter
                if self._is_vhd(image.urn):
                    marketplace_image = ""
                    vhd = image.urn
                    name = "vhd"
                else:
                    marketplace_image = image.urn
                    vhd = ""
                    match = AgentTestSuitesCombinator._URN.match(image.urn)
                    if match is None:
                        raise Exception(f"Invalid URN: {image.urn}")
                    name = f"{match.group('offer')}-{match.group('sku')}"

                # If the runbook specified a location, use it. Then try the suite location, if any. Otherwise, check if the image specifies
                # a list of locations and use any of them. If no location is specified so far, use the default.
                if self.runbook.location != "":
                    location = self.runbook.location
                elif suite_info.location != '':
                    location = suite_info.location
                elif len(image.locations) > 0:
                    location = image.locations[0]
                else:
                    location = AgentTestSuitesCombinator._DEFAULT_LOCATIONS[self.runbook.cloud]

                # If the runbook specified a VM size, use it. Else if the image specifies a list of VM sizes, use any of them. Otherwise,
                # set the size to empty and let LISA choose it.
                if self.runbook.vm_size != '':
                    vm_size = self.runbook.vm_size
                elif len(image.vm_sizes) > 0:
                    vm_size = image.vm_sizes[0]
                else:
                    vm_size = ""

                if suite_info.owns_vm:
                    # create an environment for exclusive use by this suite
                    environment_list.append({
                        "c_marketplace_image": marketplace_image,
                        "c_location": location,
                        "c_vm_size": vm_size,
                        "c_vhd": vhd,
                        "c_test_suites": [suite_info],
                        "c_env_name": f"{name}-{suite_info.name}"
                    })
                else:
                    # add this suite to the shared environments
                    key: str = f"{name}-{location}"
                    if key in shared_environments:
                        shared_environments[key]["c_test_suites"].append(suite_info)
                    else:
                        shared_environments[key] = {
                            "c_marketplace_image": marketplace_image,
                            "c_location": location,
                            "c_vm_size": vm_size,
                            "c_vhd": vhd,
                            "c_test_suites": [suite_info],
                            "c_env_name": key
                        }

        environment_list.extend(shared_environments.values())

        log: logging.Logger = logging.getLogger("lisa")
        log.info("******** Environments *****")
        for e in environment_list:
            log.info(
                "{ c_marketplace_image: '%s', c_location: '%s', c_vm_size: '%s', c_vhd: '%s', c_test_suites: '%s', c_env_name: '%s' }",
                e['c_marketplace_image'], e['c_location'], e['c_vm_size'], e['c_vhd'], [s.name for s in e['c_test_suites']], e['c_env_name'])
        log.info("***************************")

        return environment_list

    _URN = re.compile(r"(?P<publisher>[^\s:]+)[\s:](?P<offer>[^\s:]+)[\s:](?P<sku>[^\s:]+)[\s:](?P<version>[^\s:]+)")

    @staticmethod
    def _is_urn(urn: str) -> bool:
        # URNs can be given as '<Publisher> <Offer> <Sku> <Version>' or '<Publisher>:<Offer>:<Sku>:<Version>'
        return AgentTestSuitesCombinator._URN.match(urn) is not None

    @staticmethod
    def _is_vhd(vhd: str) -> bool:
        # VHDs are given as URIs to storage; do some basic validation, not intending to be exhaustive.
        parsed = urllib.parse.urlparse(vhd)
        return parsed.scheme == 'https' and parsed.netloc != "" and parsed.path != ""
