"""Utilities for parsing content from MWS responses.

XML to Dict code Borrowed from https://github.com/timotheus/ebaysdk-python
"""

from io import BytesIO
from zipfile import ZipFile
import re
import xml.etree.ElementTree as ET

from mws.errors import MWSError
from mws.utils.crypto import calc_md5


class DotDict(dict):
    """Extension of dict to allow accessing keys as attributes.

    Example:
    >>> a = DotDict()
    >>> a.fish = 'fish'
    >>> a['fish']
    'fish'
    >>> a['water'] = 'water'
    >>> a.water
    'water'
    """

    def __init__(self, initd=None):
        if initd is None:
            initd = {}
        dict.__init__(self, initd)

    def __getattr__(self, item):
        """Allow access to dict keys as though they were attributes."""
        return self.__getitem__(item)

    def __setattr__(self, item, value):
        """Allows setting dict keys like attributes, opposite of `__getattr__`."""
        self.__setitem__(item, value)

    def _value_or_node(self, node):
        """If `node` contains only a single 'value' key, returns the raw value.
        Otherwise, returns the node unchanged.
        """
        if isinstance(node, self.__class__) and "value" in node and len(node) == 1:
            return node["value"]
        return node

    def __getitem__(self, key):
        """Returns single-value nodes as the raw value, and all else unchanged."""
        node = super().__getitem__(key)
        return self._value_or_node(node)

    def __setstate__(self, item):
        return False

    def __iter__(self):
        """Nodes are iterable be default, even with just one child node.

        Returns non-list nodes wrapped in an iterator, so they can be iterated
        and return the child node.
        """
        # If the parser finds multiple sibling nodes by the same name
        # (under the same parent node), that node will return a list of DotDicts.
        # However, if the same node is returned with only one child in other responses,
        # downstream code may expect the list, but iterating the single node will
        # throw an error.
        # So, when iteration is required, we return single nodes as an iterator
        # wrapping that single instance.
        if not isinstance(self, list):
            return iter([self])
        return self

    def get(self, key, default=None):
        """Access a node like `dict.get`, including default values."""
        try:
            return self.__getitem__(key)
        except KeyError:
            return default


# DEPRECATED
ObjectDict = DotDict


def remove_xml_namespace(xml):
    """Strips the namespace from XML document contained in a string.
    Returns the stripped string.
    """
    regex = re.compile(' xmlns(:ns2)?="[^"]+"|(ns2:)|(xml:)')
    return regex.sub("", xml)


class XML2Dict(object):
    def __init__(self):
        pass

    def _parse_node(self, node):
        node_tree = DotDict()
        # Save attrs and text, hope there will not be a child with same name
        if node.text and node.text.strip():
            # Only assign a value if both the value and its `.strip`ped version work.
            # (a falsey .strip() will exclude values like "\n      ")
            node_tree.value = node.text
        for key, val in node.attrib.items():
            # if val.strip():
            key, val = self._namespace_split(key, DotDict({"value": val}))
            node_tree[key] = val
        # Save childrens
        for child in node:
            tag, tree = self._namespace_split(child.tag, self._parse_node(child))
            if tag not in node_tree:  # the first time, so store it in dict
                node_tree[tag] = tree
                continue
            old = node_tree[tag]
            if not isinstance(old, list):
                node_tree.pop(tag)
                node_tree[tag] = [old]  # multi times, so change old dict to a list
            node_tree[tag].append(tree)  # add the new one

        return node_tree

    def _namespace_split(self, tag, value):
        """Split the tag '{http://cs.sfsu.edu/csc867/myscheduler}patients'
        ns = http://cs.sfsu.edu/csc867/myscheduler
        name = patients
        """
        result = re.compile(r"\{(.*)\}(.*)").search(tag)
        if result:
            value.namespace, tag = result.groups()

        return (tag, value)

    def parse(self, filename):
        """Parse XML file to a dict."""
        file_ = open(filename, "r")
        return self.fromstring(file_.read())

    def fromstring(self, str_):
        """Convert XML-formatted string to an DotDict."""
        text = ET.fromstring(str_)
        root_tag, root_tree = self._namespace_split(text.tag, self._parse_node(text))
        return DotDict({root_tag: root_tree})


# DEPRECATION: these are old names for these objects, which have been updated
# to more idiomatic naming convention. Leaving these names in place in case
# anyone is using the old object names.
# TODO: remove in 1.0.0
object_dict = ObjectDict
xml2dict = XML2Dict


class DictWrapper(object):
    """Converts XML data to a parsed response object as a tree of `DotDict`s.

    Use `.parsed` for direct access to those contents, and `.original` for
    the original XML document string.
    """

    # TODO create a base class for DictWrapper and DataWrapper with all the keys we expect in responses.
    # This will make it easier to use either class in place of each other.
    # Either this, or pile everything into DataWrapper and make it able to handle all cases.

    def __init__(self, xml, result_key=None):
        if isinstance(xml, bytes):
            try:
                xml = xml.decode(encoding="iso-8859-1")
            except UnicodeDecodeError as exc:
                # In the very rare occurence of a decode error, attach the original xml to the .response of the MWSError
                error = MWSError(str(exc.response.text))
                error.response = xml
                raise error

        self.response = None
        self._original = xml
        self._result_key = result_key
        # TODO try this with xmltodict library?
        self._mydict = XML2Dict().fromstring(remove_xml_namespace(self.original))
        self._response_dict = self._mydict.get(
            list(self._mydict.keys())[0], self._mydict
        )

    @property
    def parsed(self):
        """Returns parsed XML contents as a tree of `DotDict`s."""
        if self._result_key:
            return self._response_dict.get(self._result_key, self._response_dict)
        return self._response_dict

    @property
    def original(self):
        """Returns original XML content."""
        return self._original


class DataWrapper(object):
    """Text wrapper in charge of validating the hash sent by Amazon."""

    def __init__(self, data, headers):
        self.original = data
        self.response = None
        self.headers = headers
        if "content-md5" in self.headers:
            hash_ = calc_md5(self.original)
            if self.headers["content-md5"].encode() != hash_:
                raise MWSError("Wrong Content length, maybe amazon error...")

    @property
    def parsed(self):
        """Returns original content.

        Used to provide an identical interface as `DictWrapper`, even if
        content could not be parsed as XML.
        """
        return self.original

    @property
    def unzipped(self):
        """Returns a `ZipFile` of file contents if response contains zip file bytes.

        Otherwise, returns None.
        """
        if self.headers["content-type"] == "application/zip":
            try:
                with ZipFile(BytesIO(self.original)) as unzipped_fileobj:
                    # unzipped the zip file contents
                    unzipped_fileobj.extractall()
                    # return original zip file object to the user
                    return unzipped_fileobj
            except Exception as exc:
                raise MWSError(str(exc))
        return None  # 'The response is not a zipped file.'
